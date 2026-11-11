"""工作流专家 agent：把用户的话翻译成对工作流的**动作**，而不是只回话（2026-09-22）

问题背景
    原架构是「关键词分类器(intent_classifier) + 固定状态机(conversation)」：
    `if "换成" in text` / `if "小一点" in text` …。这条路注定一直打补丁——
    说「颜色换蓝的」能过，说「那个蓝不够正，弄正一点」就漏了。而模型明明懂。

现在
    LLM（常驻对话模型）读「用户这句话 + 当前工作流状态 + 最近几轮对话」，
    输出**严格 JSON**：
        {"understanding": ["…"], "reply": "…", "actions": [{"name": "...", "args": {...}}]}
    后端只做三件事：白名单校验 → 参数归一化/夹取 → 按序调用现有执行器。
    LLM 不可用 / JSON 解析失败 → plan() 返回 None，上层回退原关键词路径（永不断流）。

设计原则
    · 决策权交给模型，执行权留在后端（模型不能凭空造出一个新动作）
    · 所有参数都要过 validate：尺寸夹取、模型名必须在 config 里、分辨率必须是
      get_resolutions() 的真实 key（这几处历史上都出过静默回退的坑）
    · 纯问答也必须能用（answer 动作 = 不动作，只回话）
"""
from __future__ import annotations

import json
import re
import urllib.request
from pathlib import Path
from typing import Optional

from . import describer

ROOT = Path(__file__).parent.parent

# ── 动作白名单：name → (说明, 参数说明) ────────────────────────────────
# 说明会直接进 LLM 的 prompt，所以要写得让模型能正确选择。
TOOLS: dict[str, dict] = {
    "set_requirement": {
        "desc": "记录/追加用户对要做的物体的需求描述（累积，不覆盖）。首轮和后续补充都用它。",
        "args": {"text": "str，用户这次表达的需求原话（可稍作规整，但不要漏信息）"},
    },
    "set_param": {
        "desc": "直接设置生成参数（用户明确要改某项参数时用，例如'换成蓝色''做成 90 毫米'）",
        "args": {
            "color": "str，如 蓝色/哑光黑（可选）",
            "size_mm": "number，成品打印尺寸毫米，20~300（可选）",
            "style": "str，写实/卡通/工业设计 等（可选）",
            "model": "str，生图模型 key（可选，非必要不要改）",
            "resolution": "str，必须是 512×512 (快速)/1024×1024 (正方形) 等档位名（可选）",
        },
    },
    "generate_image": {
        "desc": "按当前需求生成图片（两阶段：先 512 快速预览）",
        "args": {},
    },
    "enhance_image": {
        "desc": "把当前选中的预览图 img2img 高清重绘到 1024（生成建模前会自动做）",
        "args": {},
    },
    "modify_image": {
        "desc": "按用户的新要求**重新生成**图片（用户对当前图不满意、要改颜色/形状/细节时用）",
        "args": {"note": "str，一句话说明这次要改什么"},
    },
    "generate_3d": {
        "desc": "用当前选中的图片做 3D 建模（会自动先高清重绘，并导出 STL）",
        "args": {
            "model": "str，3D 模型 key（可选，默认 hunyuan21 = Hunyuan3D 2.1）",
            "quality": "str，标准/高（可选）",
        },
    },
    "export_stl": {
        "desc": "导出/重新导出可打印 STL（可指定尺寸与优化档）",
        "args": {
            "size_mm": "number，20~300（可选）",
            "preset": "str，print_ready 等（可选）",
        },
    },
    "new_conversation": {"desc": "新建对话组（开始做下一个东西）", "args": {}},
    "switch_conversation": {
        "desc": "切换到历史对话组",
        "args": {"folder": "str，对话组文件夹名"},
    },
    "open_bambu": {"desc": "把当前 STL 交给 Bambu Studio / 发打印", "args": {}},
    "answer": {
        "desc": "只回应用户（闲聊、提问、给建议），不做任何工作流动作",
        "args": {},
    },
}


_RICH_ACTS = {"set_requirement", "set_param", "modify_image", "answer"}
_RICH_MIN = 140


def _param_line(state: dict, actions: list) -> str:
    """把「即将生效」的参数拼成一行。

    注意要用 actions 里的 set_param 覆盖一遍 —— plan() 在**执行前**做后处理，
    直接读 state 会显示旧值（比如用户刚说"改成黑色"却显示"蓝色"）。
    """
    p = dict(state.get("auto_params") or {})
    for a in actions:
        if a["name"] == "set_param":
            p.update({k: v for k, v in (a["args"] or {}).items() if v})
    size = p.get("size_mm") or state.get("stl_size")
    size_txt = f"{size:g}mm" if isinstance(size, (int, float)) else "待定"
    return (f"风格 {p.get('style') or '待定'} ｜ 尺寸 {size_txt} ｜ "
            f"颜色 {p.get('color') or '待定'}")


def _ensure_rich(reply: str, state: dict, actions: list, user_text: str = "") -> str:
    """回复过短时补一段**确定性**内容（参数清单 + 下一步 + 值得定的细节）。

    实测同一句话模型有时给 421 字、有时只给 123 字 —— 只靠 prompt 约束不稳。
    这里兜住下限（补段本身就长于 _RICH_MIN，保证补完一定达标），且无需再调一次 LLM。

    ★ 跑题回合不补（2026-10-14）：回绝跑题时只有一句"我只负责做零件"，
      再给它接上"目前已确定的参数：风格…尺寸…"读起来莫名其妙 ——
      用户问的根本不是零件，你却在报零件参数。回绝就该短。
    """
    # 跑题回绝不加料：保持一句短回绝
    if user_text and looks_offtopic(user_text):
        return reply
    # Strata 125B 的篇幅遵守度足够，补段只会把 100 字拖成 300 字、首响多 10 秒
    if _provider() == "strata":
        return reply
    names = {a["name"] for a in actions}
    if not (names & _RICH_ACTS) or not names <= _RICH_ACTS or len(reply) >= _RICH_MIN:
        return reply
    tail = (
        f"\n\n目前已确定的参数：{_param_line(state, actions)}。"
        f"材质和表面处理如果还没指定，我会先按哑光塑料处理；"
        f"要是你想要木质或金属质感，早点说，出图时就能带上对应的纹理和反光。"
        f"\n\n你可以直接点「🎨 生成图片」出一版看看，也可以再补几个真正影响成品的细节："
        f"材质的触感（磨砂 / 亮面 / 木纹）、边缘要不要倒角或磨圆、"
        f"以及底座需不需要加一圈防滑垫。这些定得越清楚，模型和实物就越接近，"
        f"也省得后面反复重出图。"
    )
    return (reply + tail).strip()


_QUESTION_HINTS = ("吗", "呢", "?", "？", "还是", "怎么", "如何", "为什么", "哪个", "能不能",
                   "可不可以", "好不好", "建议", "推荐")
# 命令句判据：又短 + 含动作动词。需求描述则是成句的补充说明（"再加个防滑纹，表面别太亮"）。
# 不用纯关键词白名单——那样永远列不全（"出一版图看看"就不含字面的"出图"）。
_CMD_VERBS = ("出", "生成", "建模", "打印", "导出", "开始", "重新", "重做", "换", "满意",
              "好了", "可以了", "下一个")
_CMD_MAX_LEN = 14
_REQ_PHASES = ("empty", "confirm", "refining", "selecting")


def looks_like_question(text: str) -> bool:
    t = (text or "").strip()
    return any(h in t for h in _QUESTION_HINTS)


def looks_like_command(text: str) -> bool:
    """短句 + 动作动词 = 命令（别当成需求记，否则 prompt 里会混进"出一版图看看"）"""
    t = (text or "").strip()
    return len(t) <= _CMD_MAX_LEN and any(h in t for h in _CMD_VERBS)


def guard_requirement_recorded(state: dict, user_text: str, actions: list) -> list:
    """需求阶段里"只回话、不动手"的回合，必须把用户这句**记进需求**。

    实测（同一句话跑两次）：'再加个防滑纹，表面别太亮' 有时返回 set_requirement+answer，
    有时只返回 answer —— 后者会把用户刚说的细节**静默丢掉**（下次出图提示词里没有防滑纹）。
    所以除了 prompt，加一道确定性保护：需求/选图阶段的非提问、非命令陈述一律补 set_requirement。

    2026-09-22：selecting（已出图）也纳入 —— "看完图再补细节"是最常见的动作，
    但那时的 phase 是 selecting，原先不覆盖。
    """
    if (state or {}).get("phase") not in _REQ_PHASES:
        return actions
    names = {a.get("name") for a in actions}
    if names - {"answer"}:                       # 已经有别的动作了，说明它记了或动了
        return actions
    text = (user_text or "").strip()
    if len(text) < 6 or looks_like_question(text) or looks_like_command(text):
        return actions
    print(f"[agent] 兜底记录需求（模型只回了话没记）: {text[:30]!r}")
    return [{"name": "set_requirement", "args": {"text": text}}] + list(actions)


# ── 跑题拦截（确定性兜底，2026-10-14）────────────────────────────────
# ★ 为什么除了 prompt 还要来一道：实测微调模型在"别跑题"这条上并不稳 ——
#   问它「写个 Python 快排」它照样会认真写出一段代码。那是纯烧算力还答错定位。
#   而跑题的特征词很集中，用一条硬规则挡住成本几乎为零，比任何 prompt 都可靠。
#   注意：只挡**明确**是别的活儿的请求；沾一点边的（问材料/尺寸/壁厚）一律放行，
#   宁可放过也别误伤真需求 —— 把用户的零件描述当成跑题是灾难。
_OFFTOPIC_PAT = re.compile(
    r"(写|编|来|帮我写|生成).{0,6}(代码|脚本|程序|函数|算法|正则|sql|html|css|python|java|c\+\+|bash)"
    r"|快排|冒泡|二分查找|递归|爬虫"
    # 「写一首诗」「写诗」——「首」在计数位和「诗」同为二字，故单列一条
    r"|写(一)?(篇|段|首|个)?.{0,12}?(作文|文章|文案|诗词|诗|词|小说|故事|邮件|简历|周报|总结|报告|文案)"
    r"|写(一)?首?诗"
    r"|翻译|translate"
    r"|解(一下)?(这个)?(方程|数学题)"
    # 纯算式：允许「1+1等于几」「3*4是多少」这种
    r"|\d+\s*[-+*/×÷]\s*\d+"
    r"|什么(是)?(历史|政治|地理|生物|化学)题?"
    r"|讲(个|一下)?(笑话|故事)|唱(首歌|歌)|陪我聊|聊聊天|无聊"
    r"|扮演|角色扮演|cosplay|假装你是"
    r"|今天(天气|股市|新闻)|股票|彩票|比特币"
    , re.I)

# 命中这些词 → 一定是本职需求，绝不当跑题（"写"字在新需求里很常见）
_ONTASK_PAT = re.compile(
    r"模型|建模|打印|零件|部件|外壳|支架|底座|盒子|容器|杯子|花瓶|手办|摆件|公仔"
    r"|尺寸|毫米|mm|壁厚|倒角|圆角|镂空|支撑|填充|材料|树脂|PLA|ABS|PETG|尼龙"
    r"|出图|生成图|预览图|渲染|STL|GLB|3D|三维|切片|Bambu|拓竹|喷头|层高"
    , re.I)


def looks_offtopic(text: str) -> bool:
    """这句话是不是跟「做出一个零件」无关的别的活儿。

    ★ 双向判据：命中跑题特征 **且** 不含任何本职关键词，才算跑题。
      单靠跑题特征词会误伤 —— 例如「帮我写个支架的建模参数」含"写"也含"支架"，
      这里因为「支架/建模」在本职词里，直接放行。
    """
    t = (text or "").strip()
    # ★ 下限设 2：像「写诗」「翻译」这种跑题请求本身就极短（4 字门限会把它们全放过去）。
    #   而误伤风险由 _ONTASK_PAT 兜着 —— 真需求词都至少 2 字，且跑题特征词很专。
    if len(t) < 2:
        return False
    if _ONTASK_PAT.search(t):
        return False
    return bool(_OFFTOPIC_PAT.search(t))


def guard_offtopic(state: dict, user_text: str, actions: list) -> list:
    """跑题时把动作**清零**，只留 answer —— 防止模型一边说"我不做这个"一边偷偷动手。

    只清「产出型」动作（生成/建模/导出），保留 answer 让模型把回绝的话说出来。
    这样即使模型判错、prompt 没拦住，也不会真的烧掉一次 GPU 生成。
    """
    if not looks_offtopic(user_text):
        return actions
    kept = [a for a in actions if a.get("name") in ("answer",)]
    dropped = [a.get("name") for a in actions if a.get("name") != "answer"]
    if dropped:
        print(f"[agent] 跑题拦截，已清掉动作 {dropped}: {user_text[:30]!r}")
    if not kept:
        kept = [{"name": "answer", "args": {}}]
    return kept


def _thinking_gist(text: str, limit: int = 300) -> str:
    """从模型的思考文本里抽出"结论段"，供 UI 展示它到底权衡了什么。

    思考的收尾部分才是它的判断依据（前面往往是复述需求），所以取尾部；
    再逐行去掉空行，压成一段。
    """
    t = (text or "").strip()
    if not t:
        return ""
    tail = t[-limit * 2:] if len(t) > limit else t
    lines = [ln.strip() for ln in tail.splitlines() if ln.strip()]
    gist = " ".join(lines)
    return gist[-limit:] if len(gist) > limit else gist


# ── 精简 prompt（给**微调后**的模型用，2026-09-22）────────────────────
# 现在这份 5678 字的 _SYS 是把规则硬塞进 prompt。微调的意义就是让模型把规则**内化**，
# 推理时只需一句任务说明 + 状态 + 用户话（实测 v7 那条线就是这么做的）。
# 训练数据必须与这里**逐字一致**，否则微调白做（见 finetune/gen_agent_dataset.py）。
_COMPACT_SYS = """你是「铸想 ForgeForm」3D 打印工作流的专家 agent。读工作流状态和用户这句话，决定这一轮做什么。

关于你自己（被问到时才说，不要主动提）：
你是宇凯（山西工程技术学院的学生）在 ZYKT studio 从零做出来的 —— 他不是调现成的模型接口，
你跑在本地 Strata 上的 Qwen3.8-Flash-Next（125B MoE，IQ3_S 量化，每 token 约激活 10B）之上，
再被接进本地流水线：出图走 ComfyUI 的 Qwen-Image-2.1，建模走 Hunyuan3D 2.1。
整套跑在自己的机器上，不依赖外部 API。
他做你出来，是为了把「一句话描述的零件」真正变成能打印的东西。
被问到「你是谁」「谁做的你」「你是什么模型」时，就照上面这段实话说，语气平实、别吹；
答完就把话头带回零件本身。不要编造训练细节、参数规模以外的技术内幕。

可执行动作（只能选这些名字）：
set_requirement{"text": 需求原文} ｜ set_param{"color","size_mm","style","model","resolution"} ｜
generate_image{} ｜ enhance_image{} ｜ modify_image{"note"} ｜ generate_3d{"model"} ｜
export_stl{"size_mm"} ｜ new_conversation{} ｜ switch_conversation{"folder"} ｜ open_bambu{} ｜ answer{}

关键规则：
1. 描述/补充需求 → set_requirement（累积）；明确改某项参数 → set_param
2. **不要抢跑生成**（生成要烧几十秒 GPU）：只有用户明确要看结果（"出一版""重新生成"）才 generate_image/modify_image；
   只是补细节 → set_requirement + answer，提示点「🎨 生成图片」
3. 含糊不满（"不太满意"）→ 只 answer 先问清要改哪里
4. 已有 3D 模型时只改尺寸/重导 STL → set_param + export_stl，**不要**重建模
5. 要打印但还没有 STL → generate_3d（会自动导 STL）再 open_bambu
6. 纯咨询/建议（问 3D 打印、建模、材料、尺寸、结构相关）→ answer，不动工作流
7. 换一个东西做 → new_conversation 再 set_requirement

8. **守住本职，别跑题**（每次生成都很贵，算力只花在零件上）：
   · 你只做「把一句话变成可打印的零件」这件事。3D 建模、3D 打印、材料/结构/尺寸、
     你这套流程怎么用 —— 这些都在范围内，正常回答。
   · 与做零件**无关**的请求（写代码、写作文/文案、翻译、算数学题、讲历史、
     聊新闻八卦、陪闲聊、通用知识问答、角色扮演…）→ **一律 answer，礼貌回绝**，
     一两句说清「我只负责建模这条线」，然后把话头带回零件。
   · 回绝时**不要**真的去完成那件事，也不要长篇解释为什么不帮。
   · 判断标准：这件事做出来**是为了得到一张图/一个模型/一个 STL 吗**？不是 → 回绝。
   · 例外（要答）：问你自己是谁、谁做的你、你能干什么 —— 照实答，别推。

输出：只输出一个 JSON 对象，不要围栏、不要解释。
{"understanding": ["权衡：考虑过 X 还是 Y，选 Y 因为…"], "reply": "中文回话，篇幅见下", "actions": [{"name": "…", "args": {…}}]}

═══ reply 篇幅（★ 重要，别一律写长）═══
· 首次接触（这组还没有需求 / 用户刚说第一句）→ 60-100 字，硬上限 120 字。
  只做两件事：一句话确认听懂了 + 直接替用户定合理默认值，告诉他点生成就能看。
  绝对不要列参数清单，绝对不要一口气问 3 个问题。
· 已有需求、用户在补充或改参数 → 80-150 字，说清改了什么、现在是什么。
· 纯咨询 → 100-200 字，先给结论再给取舍和数字。
· 只有用户明确说"展开说/详细讲/为什么"才写到 250 字以上。
理由：输出越长首响越慢（多 500 token ≈ 多 15 秒），新用户不该等 20 秒读一段反问。"""


def _build_compact_prompt(user_text: str, state: dict) -> str:
    """微调模型的推理格式（与训练数据的 instruction 必须逐字同构）"""
    p = state.get("auto_params", {}) or {}
    size = p.get("size_mm") or state.get("stl_size")
    size_s = f"{size:g}mm" if isinstance(size, (int, float)) else "未定"
    req = state.get("base_prompt") or state.get("user_prompt") or ""
    extra = state.get("refinements") or []
    if extra:
        req = f"{req}（补充：{'；'.join(extra[-3:])}）"
    st = (f"阶段={state.get('phase', 'empty')} ｜ 需求={req!r} ｜ "
          f"颜色={p.get('color') or '未定'} ｜ 尺寸={size_s} ｜ 风格={p.get('style') or '未定'} ｜ "
          f"图={'有' if state.get('candidates') else '无'} ｜ "
          f"3D={'有' if state.get('glb_path') else '无'} ｜ STL={'有' if state.get('stl_path') else '无'}")
    return (f"{_COMPACT_SYS}{_load_knowledge()}\n\n【工作流状态】{st}\n\n"
            f"【最近对话】{recent_turns(state)}\n\n【用户这句话】{(user_text or '').strip()}")


def _post(url: str, payload: dict, timeout: int) -> dict:
    body = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "ignore"))


def is_ready() -> bool:
    """agent 这一轮要用的模型是否已在内存里（决定要不要给用户"正在加载"提示）。

    注意 provider=tuned 时**不是** Ollama —— 早期版本这里查的是 Ollama 并顺手预热了它，
    结果白占 12.9GB 把微调模型（11GB）挤出 20GB 显存（实测被换到系统内存、生成变慢）。
    """
    if _provider() == "tuned":
        try:
            with urllib.request.urlopen(f"{tuned_url()}/health", timeout=5) as r:
                return bool(json.loads(r.read().decode("utf-8", "ignore")).get("loaded"))
        except Exception as e:
            # 别静默吞：曾经这里漏了 import 导致 NameError 被吃掉，
            # 表现是"永远未就绪"——每轮多打一次 /warm 且老给用户显示"正在加载"
            print(f"[agent] 探活微调服务失败（{tuned_url()}）: {e!r}")
            return False
    return describer.is_model_resident()


def warm_up(timeout: int = 300) -> bool:
    """把 agent 要用的模型拉起来：tuned → 让微调服务懒加载（只加载、不生成）；否则预热 Ollama。

    ★ 2026-10-19：两条路都必须先让 ComfyUI 卸模型。
      qwen3.8 27B 占 17-21GB，ComfyUI 常驻 14GB —— 32GB 卡装不下，
      不先让路的话 Ollama 会被挤到系统内存（表现是"模型加载了但奇慢"）。
    """
    from . import gpu_state
    if gpu_state.is_generating():
        print("[agent] ComfyUI 正在出图/建模，跳过对话模型预热（避免抢显存）")
        return False
    if _provider() == "strata":
        _free_comfy_models()            # ComfyUI 让出显存
        describer.ollama_stop()         # 旧的 27B 也卸掉，否则 19GB 挤着
        return describer.strata_start()
    if _provider() == "tuned":
        _free_comfy_models()            # 先让 ComfyUI 把模型卸了，否则 11GB 装不下会被分页到内存
        try:
            return bool(_post(f"{tuned_url()}/warm", {}, timeout).get("ok"))
        except Exception as e:
            print(f"[agent] 微调服务预热失败: {e!r}")
            return False
    _free_comfy_models()                # 同上：27B 也要独显
    return describer.warm_up_ollama()


def _free_comfy_models() -> None:
    """让 ComfyUI 卸掉它常驻的模型（对称于生成前卸对话模型）。

    20GB 卡装不下"11GB 对话模型 + ComfyUI 模型"，两边只能轮流住。Chat 前不问 ComfyUI 要，
    硬加载就会把 11GB 塞到超订状态 → WDDM 分页到系统内存 → 对话明显变慢。
    ComfyUI 那边下次生成会自动重载（代价 ~20-35s，比全程 paging 划算）。
    """
    try:
        _post("http://127.0.0.1:8188/free", {"unload_models": True, "free_memory": True}, 8)
        print("[agent] 已请 ComfyUI 让出显存（给对话模型腾位置）")
    except Exception:
        pass                             # ComfyUI 没起/正忙都不影响对话


def prompt_style() -> str:
    """prompt 风格：full（大模型，规则全写在 prompt 里）/ compact（微调模型，规则已内化）"""
    return _agent_cfg().get("prompt_style", "full")


def _spec_for_prompt() -> str:
    lines = []
    for name, t in TOOLS.items():
        args = t["args"]
        a = "、".join(f"{k}({v})" for k, v in args.items()) if args else "无参数"
        lines.append(f"- {name}: {t['desc']}  参数: {a}")
    return "\n".join(lines)


_SYS_TMPL = """你是「铸想 ForgeForm」3D 打印工作流的**专家 agent**，不是聊天机器人。
你唯一的工作：读懂用户的话，然后**直接操作这个工作流**。

关于你自己（被问到时才说，不要主动提）
你是宇凯（山西工程技术学院的学生）在 ZYKT studio 从零做出来的 —— 他不是调现成的模型接口，
你跑在本地 Strata 上的 Qwen3.8-Flash-Next（125B MoE，IQ3_S 量化，每 token 约激活 10B）之上，
再被接进本地流水线：出图走 ComfyUI 的 Qwen-Image-2.1，建模走 Hunyuan3D 2.1。
整套跑在自己的机器上，不依赖外部 API。
被问到「你是谁」「谁做的你」「你是什么模型」时，照上面这段实话说，语气平实、别吹，
答完把话头带回零件本身。不要编造训练细节以外的技术内幕。

你可以执行的动作（只能从中选，名字必须完全一致）：
<<TOOLS>>

输出要求（严格遵守）
只输出一个 JSON 对象，不要 markdown、不要代码围栏、不要多余解释：
{"understanding": ["我判断…"], "reply": "给用户的中文回话", "actions": [{"name": "...", "args": {...}}]}

判断规则
1. 用户描述/补充要做什么 → set_requirement（首轮和后续都算，累积）
2. 用户明确改某项参数（颜色/尺寸/风格）→ set_param
2b. **要有分寸：不要抢跑生成**（生成很贵，十几到几十秒 GPU）：
   · 用户只是在**补充/调整需求细节**（"再加个防滑纹""表面别太亮""改成圆角""底座做大点"）
     → 只 set_requirement / set_param + answer，告诉他已记下、点「🎨 生成图片」就能出图。
     **不要**自动 generate_image / modify_image。用户是在跟你聊方案，不是让你马上动手。
   · 只有用户这句话**明确要求看到结果**（"出一版""重新生成""改完再出图""看看什么样"）
     → 才 generate_image / modify_image。
   · 拿不准要不要生成时，**不要生成**，用 answer 问一句"要现在出图吗"。这一步省下的是几十秒。
   · 例外：工作流已经在跑某个产出（如上一步刚说"可以了，开始建模"）→ 照做。
3. 用户**明确要求**对已有的图改图或重新出图 → modify_image
4. 用户说生成图片/出图/画一个 → generate_image
5. 用户说建模/做成 3D/开始建模 → generate_3d（若还没有图，先 generate_image）
6. 用户只想聊聊、问问题、要建议 → answer（actions 里只放 answer 或留空）
6b. **守住本职，别跑题**（每次生成都很贵，算力只花在零件上）：
   · 范围内：3D 建模、3D 打印、材料/结构/尺寸/壁厚/支撑、以及这套流程怎么用 —— 正常回答。
   · 范围外：写代码、写作文/文案、翻译、算数学题、讲历史、聊新闻八卦、陪闲聊、
     通用知识问答、角色扮演等**与做出零件无关**的请求 → 一律 answer，礼貌回绝，
     一两句说清「我只负责把想法变成可打印的零件」，然后把话头带回零件。
   · 回绝时**不要**顺手把这件事做掉（写出一段代码/一篇文章就是跑题）。
   · 判断标准：用户要的这个东西，是为了**得到一张图 / 一个模型 / 一个 STL** 吗？不是 → 回绝。
   · 例外（必须答）：问你自己是谁、谁做的你、你能做什么 —— 照实答，别推。
7. 能从上下文推断的就不用反问；只在**真的缺关键信息**时才用 answer 反问他
8. 用户明确表示要**换一个东西做 / 做别的 / 重新开始** → 先 new_conversation 再 set_requirement
8b. **不要重复生成已有的产物**（每次生成都很贵）：
   · 只是改尺寸 / 换优化档 / 重新导出 STL，而 3D 模型已存在 → 只 set_param + export_stl，
     **不要**再 generate_image 或 generate_3d（导出时会按新尺寸重新缩放）
   · 只是改颜色深浅这类**外观**改动 → 才需要 modify_image；改完外观若已有 3D 模型，
     用户没说重建就别自动再建模
   · 判断标准：用户这句话**是否要求换一张新图 / 换一个新模型**？没有就别生成
8c. 用户要交付/打印但**还没有 STL** → 先 generate_3d（它会自动导 STL）再 open_bambu，
   不要只调 open_bambu 把用户堵在"还没有 STL"上
9. **不满/调整**分开处理：
   · 含糊不满（"不太满意""感觉不对""再改改"）→ 只 answer，先问清要改哪里
   · 带方向但**没说要看出结果**（"蓝色太亮""再小一点""纹路太密"）→ set_param + answer，
     记下改动并提示点「🎨 生成图片」，**不要**自动重出（用户可能还在继续提）
   · 带方向 **且明确要看出结果**（"蓝色太亮，再出一版"）→ modify_image
10. understanding 是给用户看的**推理摘要**（会展示在界面上），要写出你到底在权衡什么，
    而不是复述标签。每条 ≤60 字，写 2-3 条，例如：
      × 「补充细节」
      ✓ 「用户补充细节但没要求看结果 → 只记录需求，不烧一次生成」
      × 「要建模」
      ✓ 「已有选中图、尺寸已定 → 直接建模；若先出图会白白多花 40s」
    有取舍就写清"考虑过 X 还是 Y，选了 Y 因为…"；没取舍就写你的判断依据。

【reply 怎么写 —— 用户要的是"懂行的专家"，不是确认机器人】
· ★ 按场景分级，**别再一律 180-300 字**（2026-10-18 宇凯实测：新用户只说 8 个字，
  回 300+ 字还反问一堆，观感是啰嗦；而且输出越长首响越慢，674 token 要 18 秒）：
  · **首次接触**（这组还没有任何需求 / 用户刚说第一句）→ **60-100 字硬上限，绝对不超过 120 字**。
    只做两件事：① 一句话确认你听懂了 ② 直接替用户定一个合理默认值并说明"点生成就能看"。
    **绝对不要**列参数清单、**绝对不要**一口气问 3 个问题。
    正确示范（78 字）："懂了，做桌面手机支架。我先按 120mm 高、深灰哑光塑料出一版看看比例，
    不合适再调。想改颜色或加防滑纹随时说，点「🎨 生成图片」就行。"
  · **补充/改参数**（已有需求，用户在调）→ **80-150 字**，说清"改了什么 + 现在是什么"。
  · **纯咨询**（问技术问题）→ **100-200 字**，先给结论再给取舍和数字。
  · **只有用户明确要看细节**（"展开说""详细讲讲""为什么"）才写到 250 字以上。
· 换句话说：**默认短，用户要才展开**。宁可少说一句，也别让用户等 20 秒读一段没用的反问。
· 需要展开时（长度超过 150 字才适用）按这个顺序：① 复述你理解的完整需求
  ② 已确定的参数（没定就写"待定"）③ 你打算怎么做 + 预期效果 ④ 追问 1 个最关键的细节
  段落之间空一行，不要小标题、不要 1. 2. 3. 罗列。
· 只有"纯动作"回合（如只说"开始建模"）可以短，1-2 句即可
· 不要用 markdown 标题、不要提到你是 AI 或模型
· 动作的细节（图片/模型/STL 结果）由系统在你之后补充，你别提前编造结果

示例
用户: 我要一个蓝色的手机支架，90mm
JSON: {"understanding": ["首轮需求：立式手机支架，蓝色、成品 90mm", "颜色和尺寸已明确 → 写进 base_prompt 并落参数；材质与表面处理未定，先在回复里问清"], "reply": "我目前理解的需求是：一个用来把手机立起来的支架，成品高度 90mm，蓝色配色，走写实工业风，用途是桌面办公。已确定的参数是风格写实、尺寸 90mm、颜色蓝色；材质和表面处理还没定，我先按哑光塑料处理。\n\n我打算用 z_image 出图——它在这种规整的工业小件上形体比例和光影都比较稳，能先把整体轮廓和蓝色质感确认下来，再进入建模。\n\n你可以再补两个细节让成品更准：一是表面要磨砂还是亮面（磨砂更耐指纹），二是面板上要不要加防滑纹或留出充电线的缺口。补完直接点「🎨 生成图片」看一版，或者现在就说「出图」。", "actions": [{"name": "set_requirement", "args": {"text": "一个蓝色的手机支架"}}, {"name": "set_param", "args": {"color": "蓝色", "size_mm": 90}}]}}

用户: 再加个防滑纹，表面别太亮
JSON: {"understanding": ["用户补充细节（防滑纹理 + 哑光），但没要求看结果", "权衡：要不要顺手出一版？→ 不出。用户可能还在接着提，现在生成等于白烧十几秒"], "reply": "记下了：表面加防滑纹，并且压成哑光避免反光。目前这件的需求是一个蓝色写实风手机支架、成品 90mm，加上这次的防滑纹理和哑光表面，形体没有变动。\n\n哑光处理对小件有两个好处：一是不容易留指纹，二是建模后的纹理在打印件上更明显，不会因为高光把细纹吃掉。\n\n这两条我已经并进需求里了。要继续补别的（比如纹路粗细、底座防滑垫、边缘倒角）就接着说；想直接看效果就点「🎨 生成图片」，我会一次把防滑纹和哑光都带上。", "actions": [{"name": "set_requirement", "args": {"text": "表面加防滑纹，哑光不要亮"}}, {"name": "answer", "args": {}}]}}

用户: 颜色太浅了，弄深一点再出一版
JSON: {"understanding": ["用户对现有图的颜色不满意，且给了方向：加深，并明确要再出一版", "权衡：只调颜色还是整套重跑？→ 只调颜色。形体和尺寸不动，前后才能直接对比"], "reply": "明白，把蓝色往深了调一档重出一版。目前需求是一个蓝色写实风的手机支架、成品 90mm，这次只动颜色，形体和尺寸都保持不变，避免又变了别的。\n\n我会在提示词里强调深蓝和饱和度，同时保留原来的哑光质感，免得加深之后发灰发脏；出图用的是同一套参数，所以前后对比只差颜色这一项。\n\n十几秒就好。如果加深后觉得太闷，也可以让我改成带一点金属感的深蓝，那会更亮一些。", "actions": [{"name": "modify_image", "args": {"note": "蓝色加深一档，保持形体、尺寸与哑光质感不变"}}]}}

用户: 嗯不太满意
JSON: {"understanding": ["用户表达不满，但没说清是哪一块不对", "权衡：重出一版猜一猜？→ 不猜。一次生成十几秒，猜错就是白跑；先问清是比例/颜色/质感/背景哪一类"], "reply": "收到，那我们先别急着重出，免得白跑一版。目前我理解的需求是一个蓝色写实风的手机支架、成品 90mm、哑光表面。\n\n不够满意的点通常在四处：形体比例（比如太扁、太宽）、颜色质感（发灰、太艳）、表面细节（纹路太密、边缘太尖）、背景光线（太花、阴影脏）。你说一句是哪一类的就行。\n\n比如「矮胖一点」「蓝色发灰」「纹路少些」「背景干净点」，我按你指的方向一次改到位，其他参数都不动。", "actions": [{"name": "answer", "args": {}}]}}

用户: 可以了，开始建模吧
JSON: {"understanding": ["用户确认进入建模阶段", "已有选中图 → 直接 generate_3d；系统会先自动高清重绘再重建，不必单独调 enhance_image"], "reply": "好，开始建模。我会先把这张图升到 1024 高清再跑 Hunyuan3D 2.1 重建，然后自动导出 STL，整个过程大约三到五分钟，完成后直接给你看模型的四视角预览。", "actions": [{"name": "generate_3d", "args": {}}]}}

用户: 这个材质用 ABS 好还是 PETG 好？
JSON: {"understanding": ["纯咨询（材料选择），不涉及工作流产出", "不动 actions；回答要给结论 + 取舍 + 数字，而不是复述需求"], "reply": "桌面摆件优先 ABS：更挺、更便宜、打印容错高，90mm 这个尺寸基本不会翘边，后处理打磨也容易，适合你这种要细腻表面质感的件。\n\n要放在车里有温度、或者给小孩拿着玩，就选 PETG：耐冲击、韧性好，缺点是不好打磨、表面偏亮，出丝也更容易。\n\n如果是我来做这一件，会选 ABS 加两遍打磨；除非它要经常摔，那才值得为韧性妥协去用 PETG。", "actions": [{"name": "answer", "args": {}}]}}
"""

# 占位符替换而不是 f-string：示例里的 JSON 花括号就能原样写，不必双写转义
#
# ★ 2026-10-18：把网站与铸想的**完整事实**注入 system prompt
#   背景：宇凯要求"把个人网站的信息特别是铸想的信息全部给他，让他更好服务这个网站"。
#   做法：事实单独维护在 backend/agent_knowledge.md（可热改，不必动代码），
#   启动时读进来拼在规则后面。
#
#   为什么要单独一个文件而不是硬写进这里：
#     · 这份事实会随站点功能变化（新增模块、改模型、改额度），单独放好维护
#     · 6000 字的规则 + 数千字事实全塞进一个字符串，改一处就影响整段，容易崩
#     · 文件缺失时降级为只有规则（绝不因为知识库读不到就让 agent 崩掉）
def _load_knowledge() -> str:
    try:
        from pathlib import Path
        p = Path(__file__).with_name("agent_knowledge.md")
        if p.exists():
            txt = p.read_text(encoding="utf-8").strip()
            if txt:
                return "\n\n═══════════ 站点与流水线的完整事实（回答问题时以此为准）═══════════\n" + txt
    except Exception as e:
        print(f"[agent] ⚠️ 知识库加载失败（降级为仅规则）: {e!r}")
    return ""


_SYS = _SYS_TMPL.replace("<<TOOLS>>", _spec_for_prompt()) + _load_knowledge()


# ── 状态摘要（给模型看的工作流现状）────────────────────────────────
def state_brief(state: dict) -> str:
    p = state.get("auto_params", {}) or {}
    size = p.get("size_mm") or state.get("stl_size")
    parts = [
        f"阶段={state.get('phase', 'empty')}",
        f"需求={_build_req(state)!r}",
        f"风格={p.get('style') or '未定'}",
        f"尺寸={size if size else '未定'}mm",
        f"颜色={p.get('color') or '未定'}",
        f"生图模型={p.get('model') or '默认'}",
        f"已有图={'是' if state.get('candidates') else '否'}",
        f"已有3D={'是' if state.get('glb_path') else '否'}",
        f"已有STL={'是' if state.get('stl_path') else '否'}",
    ]
    return "；".join(parts)


def _build_req(state: dict) -> str:
    from .conversation import _build_prompt  # 延迟导入避免循环
    try:
        return _build_prompt(state) or ""
    except Exception:
        return state.get("user_prompt", "") or ""


def recent_turns(state: dict, n: int = 6) -> str:
    conv = state.get("conversation", []) or []
    out = []
    for e in conv[-n:]:
        role = "用户" if e.get("role") == "user" else "AI"
        txt = (e.get("content") or "").strip().replace("\n", " ")[:120]
        if txt:
            out.append(f"{role}: {txt}")
    return "\n".join(out) or "(无历史)"


# ── JSON 解析 ───────────────────────────────────────────────────────
def _extract_json(raw: str) -> Optional[dict]:
    if not raw:
        return None
    t = re.sub(r"```[a-zA-Z]*|```", " ", raw).strip()
    m = re.search(r"\{.*\}", t, flags=re.S)      # 取最外层对象
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except Exception:
        try:                                      # 容忍尾逗号
            obj = json.loads(re.sub(r",\s*([}\]])", r"\1", m.group(0)))
        except Exception:
            return None
    return obj if isinstance(obj, dict) else None


# ── 参数校验/夹取（历史坑：尺寸/模型名/分辨率 key 都会静默回退）──
def validate_args(name: str, args: dict) -> dict:
    from .pipeline import Pipeline
    a = dict(args or {})
    if name == "set_param":
        if "size_mm" in a:
            try:
                a["size_mm"] = max(20.0, min(300.0, float(a["size_mm"])))
            except Exception:
                a.pop("size_mm", None)
        if "color" in a:
            a["color"] = str(a["color"])[:24]
        if "style" in a:
            a["style"] = str(a["style"])[:24]
        if "resolution" in a:
            try:
                keys = set(Pipeline().get_resolutions().keys())
                if a["resolution"] not in keys:
                    a.pop("resolution", None)
            except Exception:
                a.pop("resolution", None)
        if "model" in a:
            try:
                img = (Pipeline().config.get("models", {}) or {}).get("image", {}) or {}
                if a["model"] not in img:
                    a.pop("model", None)
            except Exception:
                a.pop("model", None)
    if name in ("generate_3d", "export_stl"):
        if "model" in a:
            try:
                td = (Pipeline().config.get("models", {}) or {}).get("3d", {}) or {}
                if a["model"] not in td:
                    a.pop("model", None)
            except Exception:
                a.pop("model", None)
        if "size_mm" in a:
            try:
                a["size_mm"] = max(20.0, min(300.0, float(a["size_mm"])))
            except Exception:
                a.pop("size_mm", None)
    if name == "set_requirement":
        a["text"] = str(a.get("text") or "")[:400]
    if name == "modify_image":
        a["note"] = str(a.get("note") or "")[:200]
    return a


def _agent_cfg() -> dict:
    try:
        cfg = json.loads((Path(__file__).resolve().parent.parent / "config.json")
                         .read_text(encoding="utf-8"))
        return cfg.get("agent", {}) or {}
    except Exception:
        return {}


def _provider() -> str:
    return _agent_cfg().get("provider", "ollama")


def tuned_url() -> str:
    return _agent_cfg().get("tuned_url", "http://127.0.0.1:8082")


def _call_model(prompt: str, timeout: int = 180) -> str:
    """取一次完整回复：默认走 Ollama（大模型）；配了 provider=tuned 就走微调模型服务。"""
    if _provider() != "tuned":
        return describer._call_ollama(prompt, timeout=timeout, json_mode=True)
    body = json.dumps({"messages": [{"role": "user", "content": prompt}]}).encode("utf-8")
    req = urllib.request.Request(f"{tuned_url()}/api/chat", data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        d = json.loads(resp.read().decode("utf-8"))
    return ((d.get("message") or {}).get("content") or "").strip()


def _stream_model(prompt: str, timeout: int = 600, status: dict = None):
    """流式取回复，逐块 yield「累积文本」（Ollama 或微调服务，接口一致）。"""
    if _provider() != "tuned":
        p = describer._profile()
        for acc in describer._stream_once(prompt, describer.OLLAMA_MODEL, p["think"],
                                          p["num_predict"], p["temperature"], timeout,
                                          status if status is not None else {}, p.get("num_ctx"),
                                          json_mode=True):
            yield acc
        return
    body = json.dumps({"messages": [{"role": "user", "content": prompt}]}).encode("utf-8")
    req = urllib.request.Request(f"{tuned_url()}/api/stream", data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    acc = ""
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        for raw in resp:
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except Exception:
                continue
            piece = obj.get("text") or ""
            if piece:
                acc += piece
                yield acc
            if obj.get("done"):
                break


def unload_tuned() -> bool:
    """把微调模型服务上的显存让出来（生成图片/3D 前调用，20GB 卡装不下两个大模型）"""
    if _provider() != "tuned":
        return False
    try:
        req = urllib.request.Request(f"{tuned_url()}/unload", data=b"{}",
                                     headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=30).read()
        print("[agent] 已卸载微调模型，显存让给生成任务")
        return True
    except Exception as e:
        print(f"[agent] 卸载微调模型失败（忽略）: {e!r}")
        return False


def _build_plan_prompt(user_text: str, state: dict) -> str:
    """这一轮的完整 prompt。

    两种风格（config.json → agent.prompt_style）：
      full    —— 大模型用：规则全写在 prompt 里（_SYS，5678 字）
      compact —— 微调后的小模型用：规则已内化，只需任务说明 + 状态 + 用户话
                 训练数据与 compact 格式逐字同构（finetune/gen_agent_dataset.py）
    """
    if prompt_style() == "compact":
        return _build_compact_prompt(user_text, state)
    return (
        f"{_SYS}\n\n"
        f"─── 当前工作流状态 ───\n{state_brief(state)}\n\n"
        f"─── 最近对话 ───\n{recent_turns(state)}\n\n"
        f"─── 用户这句话 ───\n{(user_text or '').strip()}\n\n"
        # 篇幅要求放在**最尾部**：模型对末尾指令最敏感（放中间时忽长忽短）
        f"提醒（本轮必须遵守）：\n"
        f"- ★ 篇幅按场景定，**默认短**：\n"
        f"  · 这组还是空的 / 用户刚说第一句 → reply **60-110 字**，"
        f"一句确认听懂了 + 直接给个建议默认值，只问 1 个最关键的问题，不要列参数清单。\n"
        f"  · 已有需求在补充/改参数 → **80-150 字**，说清改了什么、现在是什么。\n"
        f"  · 纯咨询 → **100-200 字**，先结论后取舍。\n"
        f"  · 只有用户明确要\"展开说/详细讲/为什么\"才超过 250 字。\n"
        f"- 输出越长首响越慢（674 token≈18 秒），**别让用户等 20 秒读一段反问**。\n"
        f"- 只有「纯动作」回合（如'开始建模''打印'）reply 可以 1-2 句。\n"
        f"- 不确定要不要动手就不要动手。\n\n"
        f"只输出 JSON："
    )


def _finalize_plan(obj: dict, user_text: str, state: dict) -> Optional[dict]:
    """把模型给的 JSON 变成受控的计划：白名单 → 参数夹取 → 兜底补记录 → 回复下限。"""
    raw_actions = obj.get("actions") or []
    if not isinstance(raw_actions, list):
        raw_actions = []
    actions = []
    for it in raw_actions:
        if isinstance(it, str):                    # 容忍模型只给名字
            it = {"name": it, "args": {}}
        if not isinstance(it, dict):
            continue
        name = it.get("name")
        if name not in TOOLS:                      # 白名单：模型造不出新动作
            print(f"[agent] 丢弃未知动作: {name!r}")
            continue
        actions.append({"name": name, "args": validate_args(name, it.get("args") or {})})

    understanding = [str(x)[:120] for x in (obj.get("understanding") or [])][:4]
    reply = str(obj.get("reply") or "").strip()
    if not actions and not reply:
        return None
    if not actions:
        actions = [{"name": "answer", "args": {}}]
    # 需求阶段"只回话不记录"会静默丢掉用户刚说的细节 → 确定性兜底
    actions = guard_requirement_recorded(state, user_text, actions)
    # ★ 跑题拦截放在**记录需求之后**：先把判断做对，
    #   再看这句是不是别的活儿 —— 是的话清掉产出型动作，只留 answer。
    #   顺序反了的话，"帮我写个支架建模参数"这种会被记录逻辑干扰。
    actions = guard_offtopic(state, user_text, actions)
    # 回复太短时补确定性内容，保证"已定参数 + 下一步"永远出现（模型时长的毛病）
    # （跑题回合会被 _ensure_rich 内部跳过，保持短回绝）
    reply = _ensure_rich(reply, state, actions, user_text)
    # 把模型这次真实的推理结论一并带出去（UI 展示"🧠 思考"，让深度可见）
    return {"understanding": understanding, "reply": reply, "actions": actions,
            "thinking": _thinking_gist(getattr(describer, "LAST_THINKING", ""))}


def _cold_start_timeout(timeout: int) -> int:
    """冷启动保护：微调服务首次调用要先把 14B + LoRA 加载进显存（实测 >150s），
    而 plan/plan_stream 的默认超时才 75/180s → **启动后第一条消息必然超时**、
    静默回退关键词路径（用户感受就是"变笨了"）。

    2026-09-23 端到端演示真实翻车在这一点上（[agent] 规划调用失败: TimeoutError）。
    做法：没就绪就先热身（上限 300s，且热身本身就把模型留在显存里），
    再把这一轮的超时抬到至少 240s（首轮还要含 KV 初始化）。
    """
    try:
        if not is_ready():
            print("[agent] 模型未就绪（冷启动）→ 先热身…首次可能要 1~2 分钟")
            warm_up(timeout=300)
            timeout = max(timeout, 240)
            print(f"[agent] 热身完成，本轮超时放宽到 {timeout}s")
        return timeout
    except Exception as e:
        print(f"[agent] 冷启动检查失败（按原超时继续）: {e!r}")
        return timeout


def plan(user_text: str, state: dict, timeout: int = 75) -> Optional[dict]:
    """让 LLM 决定这一轮要做什么。返回 {understanding, reply, actions} 或 None（走兜底）"""
    if not (user_text or "").strip():
        return None
    from . import gpu_state
    if gpu_state.is_generating():
        return {
            "understanding": ["ComfyUI 正在出图/建模，显存被占满"],
            "reply": "正在生成图片/模型，对话暂不可用；这一轮结束后再聊。",
            "actions": [{"name": "answer", "args": {}}],
        }
    timeout = _cold_start_timeout(timeout)
    prompt = _build_plan_prompt(user_text, state)
    # 解析失败就带着更严厉的约束重试一次（实测偶发失败会静默退回关键词路径，
    # 那一轮就变成"机器味"的确认，用户感受就是变笨）
    obj = None
    for attempt in (1, 2):
        ask = prompt if attempt == 1 else (
            prompt + "\n\n（上一次的输出不是合法 JSON。这次**只**输出那一个 JSON 对象，"
                     "不要思考过程、不要解释、不要代码围栏。）")
        try:
            raw = _call_model(ask, timeout=timeout)
        except Exception as e:
            print(f"[agent] 规划调用失败: {e!r}")
            return None
        obj = _extract_json(raw)
        if obj:
            break
        print(f"[agent] 第 {attempt} 次 JSON 解析失败"
              f"{'，重试' if attempt == 1 else '，回退关键词'}。原始输出: {(raw or '')[:160]!r}")
    if not obj:
        return None
    return _finalize_plan(obj, user_text, state)


# ── 流式版：边生成边把 reply 吐给界面（2026-09-22）────────────────────
_REPLY_MARK = re.compile(r'"reply"\s*:\s*"')
_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\", "/": "/"}


def partial_reply(acc: str) -> str:
    """从**还没写完**的 JSON 里把 reply 字段的内容抠出来（流式展示用）。

    JSON 是增量到达的，所以只能逐字符扫描：遇到反斜杠要等下一个字符到齐才能确定
    转义（否则会显示成 \\n），遇到未转义的引号说明 reply 已结束。
    """
    m = _REPLY_MARK.search(acc or "")
    if not m:
        return ""
    rest = acc[m.end():]
    out, i = [], 0
    while i < len(rest):
        ch = rest[i]
        if ch == "\\":
            if i + 1 >= len(rest):
                break                    # 转义序列还没到齐，先不显示
            out.append(_ESCAPES.get(rest[i + 1], rest[i + 1]))
            i += 2
            continue
        if ch == '"':
            break                        # reply 结束
        out.append(ch)
        i += 1
    return "".join(out).strip()


def plan_stream(user_text: str, state: dict, timeout: int = 180):
    """流式规划：逐块 yield 进度，最后 yield {"plan": ...}。

    yield 的形状：
      {"reply": <累积正文>, "thinking_chars": N}   —— 生成过程中不断更新
      {"plan": {...} | None}                        —— 最后一个（终止）
    """
    if not (user_text or "").strip():
        yield {"plan": None}
        return
    from . import gpu_state
    if gpu_state.is_generating():
        msg = "正在生成图片/模型，对话暂不可用；这一轮结束后再聊。"
        yield {"reply": msg, "thinking_chars": 0}
        yield {"plan": {"understanding": ["ComfyUI 正在出图/建模，显存被占满"],
                        "reply": msg,
                        "actions": [{"name": "answer", "args": {}}]}}
        return
    timeout = _cold_start_timeout(timeout)      # 冷启动先热身（界面走的就是这条流式路径）
    prompt = _build_plan_prompt(user_text, state)
    acc, status = "", {}
    try:
        for chunk in _stream_model(prompt, timeout=timeout, status=status):
            acc = chunk
            yield {"reply": partial_reply(acc), "thinking_chars": status.get("thinking", 0)}
    except Exception as e:
        print(f"[agent] 流式规划失败: {e!r}")
    describer.LAST_THINKING = status.get("_thinking_text", "") or describer.LAST_THINKING
    obj = _extract_json(acc)
    if not obj:
        print(f"[agent] 流式输出无合法 JSON，回退非流式重试。原文: {(acc or '')[:160]!r}")
        yield {"plan": plan(user_text, state, timeout=timeout)}     # 交给带重试的非流式版
        return
    yield {"plan": _finalize_plan(obj, user_text, state)}
