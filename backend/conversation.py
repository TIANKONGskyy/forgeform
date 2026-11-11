"""对话状态机：管理整个对话流程"""
import json, base64, time, re, shutil, threading
from pathlib import Path
from enum import Enum
from typing import Optional

from . import project_manager as pm
from . import intent_classifier as ic
from . import describer
from . import agent


def _free_tuned_for_gpu_work(what: str = "生成") -> None:
    """生成图片/3D 前，把微调模型让出显存（20GB 卡放不下 11GB 微调模型 + ComfyUI 模型）。

    为什么必须放这里：agent 执行器（_execute_plan）里那处让显存只覆盖 agent 路径；
    用户点「🎨 生成图片」「🧊 生成模型」按钮时走的是 _do_generate_images/_do_generate_3d，
    **绕过**了执行器 —— 于是微调模型一直占着 11GB，ComfyUI 只剩几 GB 可用，
    跑着跑着就超时（实测：高清重绘 600s 超时、3D 建模 1200s 超时，而之前是 86s 出模型）。
    """
    try:
        if agent.unload_tuned():
            print(f"[DSH] 已让出微调模型显存（{what}需要）", flush=True)
    except Exception as e:
        print(f"[DSH] 让出微调显存失败（不影响{what}）: {e!r}")
    # 防御：Ollama 那份常驻模型（gpt-oss 12.9GB）同样会挤掉 ComfyUI 的显存，
    # 尤其 provider=ollama 时聊天会把 gpt-oss 常驻 —— 生成前一并卸掉。
    try:
        describer.unload_ollama()
    except Exception:
        pass


def _free_vram_gb() -> float:
    """几 GB 空闲显存（走 nvidia-smi，不引入 torch 依赖）"""
    try:
        import subprocess
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader"],
                             capture_output=True, timeout=10).stdout.decode("utf-8", "ignore")
        return int(re.sub(r"\D", "", out) or 0) / 1024.0
    except Exception:
        return 0.0


def _rewarm_tuned_async() -> None:
    """生成结束后台把对话模型加载回来，免得下一轮对话干等重载。

    2026-10-19：两种 provider 都管 —— tuned（Qwen3-14B，13GB）与
    ollama（qwen3.8 27B，17-21GB）。**必须看余量**：生成完 ComfyUI 的模型往往
    还占着，这时硬加载会装不下，Windows 的 WDDM 会把它分页到系统内存
    （显存看着满了、对话反而更慢）。余量不够就跳过，宁可下一轮懒加载一次。
    """
    from .gpu_state import is_generating
    if is_generating():
        return
    provider = agent._provider()
    need_gb = 13.0 if provider == "tuned" else 20.0   # 27B 17GB + 余量
    label = "微调 14B" if provider == "tuned" else "qwen3.8 27B"

    # 先让 ComfyUI 把模型卸掉再判余量（2026-09-23 宇凯："对话加载太慢，最好一开就常驻"）
    # 以前直接判余量：生成完 ComfyUI 通常还占着 7GB+ → free 只有 ~9-11GB < 13GB → 每次都被跳过 ✗
    # → 对话模型永远回不来，下一轮又冷加载 ~2 分钟。代价是下次生图要重载它（~20-35s），
    # 但对话是高频、生图是低频，这个取舍明显划算。
    try:
        agent._free_comfy_models()
    except Exception as e:
        print(f"[DSH] 回热前释放 ComfyUI 模型失败（忽略）: {e!r}", flush=True)

    free_gb = _free_vram_gb()
    if free_gb < need_gb:
        print(f"[DSH] 显存余量 {free_gb:.1f}GB < {need_gb:.0f}GB，跳过{label}回热"
              f"（下轮对话会懒加载一次）", flush=True)
        return

    def _run():
        try:
            if not agent.is_ready():
                agent.warm_up()
                print(f"[DSH] {label} 已后台预热（下一轮对话免重载）", flush=True)
        except Exception as e:
            print(f"[DSH] {label} 后台预热失败（不影响使用）: {e!r}")

    threading.Thread(target=_run, daemon=True).start()


_WARM_INFLIGHT = threading.Event()


def _warm_image_model_async(model_key: str) -> None:
    """3D 结束后台预热生图模型。

    generate_3d 会先 _free_vram(unload_models=True) 把生图模型也卸掉，
    导致下一个建模的首次生图要等 ~20-35s 重载。这里在 3D 完成后后台跑一个
    1 步 256×256 的最小生成把模型重新载入，纯加载、不改显存策略。

    去重（2026-09-23 修）：GPU 忙时每一次预热都会在 ComfyUI 队列里排一个新作业，
    实测堆积过 5 个 warmup 在偷 GPU。同一时刻只允许一个在飞，且队列里已有 warmup 就跳过。
    """
    if _WARM_INFLIGHT.is_set():
        return
    _WARM_INFLIGHT.set()

    def _run():
        try:
            from .pipeline import Pipeline
            p = Pipeline()
            # 队列里已经有预热作业？直接跳过（否则重复排队，实测堆过 5 个在偷 GPU）
            try:
                import requests as _rq
                # 用 getattr 兜一下：后端实现（比如 HybridBackend）不一定有 _pick_url，
                # 缺了会抛 AttributeError 被下面的 except 吞掉 —— 那等于"队列忙也不跳过"，
                # 预热照样提交（verify 的 warmup_deduped 抓的就是这个）。
                _base = getattr(p.comfy, "_pick_url", None)
                _url = _base("image") if callable(_base) else "http://127.0.0.1:8188"
                q = _rq.get(f"{_url}/queue", timeout=8).json()
                if q.get('queue_running') or q.get('queue_pending'):
                    print("[DSH] 队列忙，跳过生图预热", flush=True)
                    return
            except Exception:
                pass                     # 查不到队列就照常预热，不因查询失败而不预热
            p.generate_images("warmup", count=1, model_key=model_key,
                              resolution="512×512 (快速)", steps_override=1,
                              skip_cache=True)
        except Exception as e:
            print(f"[DSH] 生图模型后台预热失败（忽略）: {e!r}")
        finally:
            _WARM_INFLIGHT.clear()

    threading.Thread(target=_run, daemon=True).start()


class Phase(Enum):
    EMPTY     = "empty"      # 初始空白
    CONFIRM   = "confirm"    # 需求确认中
    REFINING  = "refining"   # 细化要求中（点"继续细化要求"后，等待用户输入细化内容）
    GENERATING = "generating" # 正在生成图片
    SELECTING = "selecting"  # 等待选图
    EDITING   = "editing"    # 图片修改中
    PHASE_3D  = "3d"        # 3D建模中
    STL       = "stl"        # STL导出/打印
    DONE      = "done"       # 完成


# 自动参数（阶段0生成候选图时用）
STYLE_KEYWORDS = {
    "写实":   ["写实", "真实", "照片级", "逼真"],
    "卡通":   ["卡通", "动漫", "二次元", "Q版", "可爱"],
    "低多边形": ["低多边形", "lowpoly", "low poly", "几何"],
    "水墨":   ["水墨", "国风", "水彩"],
    "机械":   ["机械", "机甲", "科幻", "赛博"],
}

# 2026-09-25 修：MODEL_HINTS 之前全 hardcode z_image，agent 选模型完全不看 config.json
# —— 即使 config.default_model = "qwen21_ref" 也用不上。
# 现在 MODEL_HINTS 只存"风格提示词增强"（不影响 model key 选取）。
# 模型 key 由 _pick_image_model() 从 config.json 读。
MODEL_HINTS = {
    "熊大": ("卡通风格", "色彩饱和", "角色一致性好", "Boonie Bears 动画风格"),
    "熊二": ("卡通风格", "色彩饱和", "角色一致性好", "Boonie Bears 动画风格"),
    "机器猫": ("卡通风格", "色彩饱和", "角色一致性好"),
    "哆啦A梦": ("卡通风格", "色彩饱和", "角色一致性好"),
    "皮卡丘": ("卡通风格", "色彩饱和", "角色一致性好"),
    "蜘蛛侠": ("写实风格", "细节还原", "动态姿势"),
    "机械": ("机械/科幻", "细节丰富", "金属质感"),
    "高达": ("机甲风格", "细节丰富", "工业设计"),
}


def _pick_image_model() -> str:
    """从 config.json 读 stages.image.default_model。fallback z_image（保证不崩）。"""
    try:
        cfg_path = Path(__file__).resolve().parent.parent / "config.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        m = cfg.get("stages", {}).get("image", {}).get("default_model")
        if m:
            return m
    except Exception:
        pass
    return "z_image"


def _auto_style(text: str) -> str:
    t = text.lower()
    for style, kws in STYLE_KEYWORDS.items():
        if any(k in t for k in kws):
            return style
    return "写实"


def _auto_model(text: str) -> str:
    """返回 config.json 里真实存在的 image 模型 key。

    2026-09-25 修：不再 hardcode z_image；默认走 config.json → stages.image.default_model
    （用户配 qwen21_ref 就走 qwen21_ref、配 z_image 就走 z_image）。
    MODEL_HINTS 只决定"风格提示词"（refinements），不影响 model key 选取。
    """
    return _pick_image_model()


def _auto_style_phrases(text: str) -> list[str]:
    """提取用户文本里的 IP/类目关键词 → 返回风格提示短语列表（被 prompt 扩写用）。
    不再影响 model key。"""
    t = text or ""
    found = []
    for keyword, hints in MODEL_HINTS.items():
        if keyword in t:
            for h in hints:
                if h not in found:
                    found.append(h)
    return found


_NUM_UNIT_RE = re.compile(r"\d+(?:\.\d+)?\s*(?:mm|cm|毫米|厘米|°|度|%)", re.I)


def _numbers_preserved(user_text: str, rewritten: str) -> bool:
    """用户原文里的「数字+单位」是否都原样出现在模型改写后的文本里。

    2026-10-19 实测：qwen3.8 把「壁厚3mm，高100mm」改写成「壁厚100mm，高100mm」，
    后续出图/3D 全按 100mm 壁厚走。数字是硬约束，模型改写不能动。
    """
    def _norm(s: str) -> set:
        return {re.sub(r"\s+", "", x).lower() for x in _NUM_UNIT_RE.findall(s or "")}

    need = _norm(user_text)
    return need.issubset(_norm(rewritten)) if need else True


def _extract_size_mm(text: str) -> Optional[float]:
    """从文本中提取打印尺寸（mm）。

    ★ 2026-10-19：先剔掉「壁厚/厚度」—— 它们是结构参数，不是整体尺寸。
      实测「壁厚3mm，高100mm」被当成 3mm 整体尺寸，模型按 3mm 缩放。
    """
    text = re.sub(r"(?:壁厚|厚度)\s*\d+(?:\.\d+)?\s*(?:mm|毫米)", " ", text or "")
    patterns = [
        r"(\d+)\s*mm",
        r"大?[小大中高矮长宽]?\s*尺?[寸]?\s*[（(]?\s*(\d+)\s*~?\s*(\d+)?\s*[mm厘米]?\s*[)）]?",
        r"大约?\s*(\d+)\s*~?\s*(\d+)?\s*[mm厘米]?",
    ]
    for p in patterns:
        m = re.search(p, text)
        if m:
            nums = [int(x) for x in m.groups() if x]
            if nums:
                # 找最像尺寸的那个数
                for num in nums:
                    if 5 <= num <= 500:
                        return float(num)
    return None


# 颜色词表（按优先顺序，长词优先匹配）
COLOR_WORDS = [
    "深蓝", "浅蓝", "天蓝", "宝蓝", "蓝色", "蓝",
    "深红", "浅红", "鲜红", "红色", "红",
    "深绿", "浅绿", "草绿", "绿色", "绿",
    "深黄", "浅黄", "黄色", "黄",
    "黑色", "黑", "白色", "白", "灰色", "灰",
    "橙色", "橙", "紫色", "紫", "粉色", "粉",
    "棕色", "咖啡色", "棕", "银色", "银", "金色", "金",
    "透明", "原色", "青色", "青",
]


def _extract_color_explicit(text: str) -> Optional[str]:
    """从文本中显式提取颜色，如「颜色改成蓝色」→「蓝色」。未提到颜色返回 None。

    只匹配「X色」形式；光杆单字（「颜色改成蓝」）要求后一个字不能把它拼成
    别的词——否则「表面用**金**属质感」会被误判成「金色」（2026-09-22 修）。
    """
    for w in COLOR_WORDS:
        if w.endswith("色") and w in text:
            return w
    # 「颜色改成蓝」这类光杆单字；后接 属/行/幕/河/鱼/牌/山/矿 等非颜色词则不算
    m = re.search(r"(?:颜色|改成|换成|用|要)\s*([红蓝绿黄黑白灰橙紫粉棕银金青])"
                  r"(?![属行幕河鱼牌山矿匙雾水])", text)
    if m:
        return m.group(1) + "色"
    return None


def _extract_style_explicit(text: str) -> Optional[str]:
    """只在文本明确提到风格词时返回风格，否则返回 None（用于保留原风格）。"""
    t = text.lower()
    for style, kws in STYLE_KEYWORDS.items():
        if any(k in t for k in kws):
            return style
    return None


def _extract_relative_size(text: str, current_mm: float) -> Optional[float]:
    """处理相对尺寸表达：「小一点」「大一些」「放大」等，返回新尺寸。"""
    small_kws = ["小一点", "小一些", "小点", "小巧", "缩小", "小一号", "小一点儿", "小一些儿"]
    big_kws = ["大一点", "大一些", "大点", "放大", "大一号", "大一点儿"]
    if any(k in text for k in small_kws):
        return max(20.0, round(current_mm * 0.7, 1))
    if any(k in text for k in big_kws):
        return min(500.0, round(current_mm * 1.4, 1))
    return None


def _build_prompt(state: dict) -> str:
    """用 base_prompt + refinements 拼出干净的生成 prompt（不含'细化要求：'等标记文字）"""
    base = state.get("base_prompt") or state.get("user_prompt", "")
    refs = state.get("refinements", [])
    if refs:
        return "，".join([base] + [r for r in refs if r])
    return base


# 纯"不满"表达（没有可执行信息）——这类不能塞进生成 prompt
_COMPLAINT_WORDS = ("有问题", "不行", "不好", "不好看", "不满意", "难看", "丑",
                    "不对", "错了", "不像", "模糊", "奇怪")
_ACTIONABLE_WORDS = ("颜色", "色", "尺寸", "大", "小", "放大", "缩小", "改", "换",
                     "加", "去掉", "删", "背景", "材质", "风格", "写实", "卡通", "细节")


def _apply_refinement(state: dict, text: str) -> list:
    """把一句修改要求**真正落到 state**（refinements + auto_params + prompt），返回改动说明。

    2026-09-22 修（宇凯报「说了图片有问题他也没修改」）：
    原 `editing` 分支只把「。修改要求：X」追加到 user_prompt，而 `_build_prompt()`
    取的是 base_prompt + refinements —— 于是修改**永远进不了生成 prompt**。
    现象是：图确实换了，但那只是随机种子的差异，颜色/尺寸完全没用你的要求。
    现在 refining / selecting / editing 三处统一走这个函数。
    """
    state["base_prompt"] = state.get("base_prompt") or state.get("user_prompt", "")
    params = state.setdefault("auto_params", {}) or {}
    cur_size = float(params.get("size_mm") or state.get("stl_size") or 120)
    changes = []

    # 1) 风格：仅在明确提到风格词时才改（避免整段重判覆盖已有风格）
    new_style = _extract_style_explicit(text)
    if new_style and new_style != params.get("style"):
        changes.append(f"风格：{params.get('style', '未指定')} → {new_style}")
        params["style"] = new_style

    # 2) 颜色：只认显式「X色」（避免「金属」误判「金色」）
    new_color = _extract_color_explicit(text)
    if new_color and new_color != params.get("color"):
        old_color = params.get("color")
        changes.append(f"颜色：{old_color or '未指定'} → {new_color}")
        params["color"] = new_color
        # 同步替换 base_prompt 里的旧颜色：否则 prompt 会同时出现「蓝色…红色的配色」，
        # 扩散模型会给出含糊结果（这是"改了颜色但看着没改"的第二个原因）
        if old_color and old_color != "未指定（可后续补充）" and old_color in state["base_prompt"]:
            state["base_prompt"] = state["base_prompt"].replace(old_color, new_color)

    # 3) 尺寸：绝对（80mm）优先，其次相对（大一点/小一点）
    abs_mm = _extract_size_mm(text)
    rel_mm = _extract_relative_size(text, cur_size) if not abs_mm else None
    new_size = abs_mm or rel_mm
    if new_size and new_size != cur_size:
        changes.append(f"尺寸：{cur_size:g}mm → {new_size:g}mm")
        params["size_mm"] = new_size
        state["stl_size"] = new_size
        # 同理：base_prompt 里的旧「整体尺寸」要跟着换 ——
        # ★ 但不能碰壁厚/厚度/直径/半径/高/宽/长这些结构尺寸（实测：壁厚3mm 被改成 100mm）
        if abs_mm and re.search(r"\d+\s*mm", state["base_prompt"]):
            _hold = {}

            def _stash(m):
                key = f"__KEEPMM{len(_hold)}__"
                _hold[key] = m.group(0)
                return key

            _masked = re.sub(
                r"(?:壁厚|厚度|直径|半径|高度|宽度|长度|高|宽|长)\s*\d+(?:\.\d+)?\s*mm",
                _stash, state["base_prompt"])
            _masked = re.sub(r"\d+(?:\.\d+)?\s*mm", f"{abs_mm:g}mm", _masked, count=1)
            for _k, _v in _hold.items():
                _masked = _masked.replace(_k, _v)
            state["base_prompt"] = _masked

    params["model"] = _auto_model(_build_prompt(state) + " " + text)
    state["auto_params"] = params

    # 4) 可执行内容进 refinements（会被拼进生成 prompt）；纯抱怨不污染 prompt
    pure_complaint = any(c in text for c in _COMPLAINT_WORDS) and \
                     not any(a in text for a in _ACTIONABLE_WORDS)
    if text and not pure_complaint:
        state.setdefault("refinements", []).append(text)
    state["user_prompt"] = _build_prompt(state)
    state["understood"] = state["user_prompt"]
    return changes


# 两阶段生成的分辨率档（必须是 pipeline.get_resolutions() 里的精确 key，
# 写错会静默回退 1024）
_PREVIEW_RES = "512×512 (快速)"
_HD_RES = "1024×1024 (正方形)"


_AGENT_CFG_CACHE: Optional[bool] = None


def agent_enabled() -> bool:
    """agent 开关（config.json → agent.enabled，默认开）。

    关掉即退回原来的关键词+状态机路径 —— 出问题时一键兜底。
    """
    global _AGENT_CFG_CACHE
    if _AGENT_CFG_CACHE is not None:
        return _AGENT_CFG_CACHE
    try:
        cfg = json.loads((Path(__file__).parent.parent / "config.json")
                         .read_text(encoding="utf-8"))
        _AGENT_CFG_CACHE = bool(cfg.get("agent", {}).get("enabled", True))
    except Exception:
        _AGENT_CFG_CACHE = True
    return _AGENT_CFG_CACHE


def _is_requirement_followup(state: dict) -> bool:
    """这组对话里是否已经表达过需求？

    True → 本轮是「补充/修改」，必须**累积**（否则前面说的话就被覆盖丢掉了）。
    2026-09-22：判据只看 base_prompt，**不看 phase** —— 原实现依赖调用方先把 phase
    推进到 confirm，若中途还没推进（如流式结束后才写 phase），第二轮会被误判成
    首轮而重置需求。
    """
    return bool((state.get("base_prompt") or "").strip())


class ConversationManager:
    """对话管理器"""

    def __init__(self, project=None, interactive=True):
        # Engine jobs own their project and must not load/repair another chat.
        self.current_project = project
        self._interactive = interactive
        if project is None:
            self._init_current_project()

    def _init_current_project(self):
        """初始化或加载最近项目"""
        projects = pm.list_projects()
        # 找最新的未完成项目
        active = [p for p in projects if p.get("phase") not in ("done", "archived")]
        if active:
            folder = active[0]["folder"]
            state = pm.load_project(folder)
            # 脏状态修复：generating/selecting 但无候选图 → 回退 confirm
            if state.get("phase") in ("generating", "selecting") and not state.get("candidates"):
                state["phase"] = "confirm"
                pm.save_project(state)
            self.current_project = state
        else:
            self.new_project()

    # ─── 项目管理 ────────────────────────────────────────────

    def new_project(self) -> dict:
        """创建全新对话组"""
        # 保存当前项目（如果存在）
        if self.current_project and self.current_project.get("folder"):
            pm.save_project(self.current_project)
            pm.archive_project(self.current_project["folder"])

        state = pm.create_project()
        self.current_project = state
        return self.current_project

    def list_projects(self) -> list[dict]:
        return pm.list_projects()

    def switch_project(self, folder_name: str) -> dict:
        """切换到指定项目。

        2026-09-23 实测踩到（app 日志里现场抓到）：Gradio 的 history_dropdown 在页面刷新/
        选项清空后会以 **None** 触发 change 事件 → 原来直接 `pm.load_project(None)` →
        `TypeError: unsupported operand type(s) for /: 'WindowsPath' and 'NoneType'`，
        界面上就是一条报错。这里空值一律"不动"，别把 None 往下传。
        """
        if not folder_name or not str(folder_name).strip():
            print("[DSH] 切换项目收到空值（多半是下拉框刷新）→ 保持当前项目不动")
            return self.current_project or {}
        if self.current_project and self.current_project.get("folder"):
            pm.save_project(self.current_project)
        self.current_project = pm.load_project(folder_name)
        return self.current_project

    # ─── 对话主入口 ──────────────────────────────────────────

    # ─── 专家 agent：让 LLM 决定这一轮做什么（2026-09-22）─────────────

    def _apply_param_values(self, state: dict, args: dict) -> list:
        """把 agent 给的**显式参数**落到 auto_params（返回改动说明）

        与 _apply_refinement 的区别：那个从自然语言里抽，这个拿到的是已归一化的值。
        同样要把 base_prompt 里的旧颜色/旧尺寸替换掉，否则 prompt 会自相矛盾
        （旧实现就因此生成过"蓝色…红色的配色"）。
        """
        p = state.setdefault("auto_params", {}) or {}
        changes = []
        if args.get("color") and args["color"] != p.get("color"):
            old = p.get("color")
            changes.append(f"颜色：{old or '未指定'} → {args['color']}")
            p["color"] = args["color"]
            base = state.get("base_prompt") or ""
            if old and old in base:
                state["base_prompt"] = base.replace(old, args["color"])
        if args.get("style") and args["style"] != p.get("style"):
            changes.append(f"风格：{p.get('style') or '未指定'} → {args['style']}")
            p["style"] = args["style"]
        if args.get("size_mm"):
            new_size = float(args["size_mm"])
            cur = float(p.get("size_mm") or state.get("stl_size") or 120)
            if abs(new_size - cur) > 0.5:
                changes.append(f"尺寸：{cur:g}mm → {new_size:g}mm")
                base = state.get("base_prompt") or ""
                state["base_prompt"] = re.sub(r"\d+(?:\.\d+)?\s*mm", f"{new_size:g}mm", base, count=1)
            p["size_mm"] = new_size
            state["stl_size"] = new_size
        for k in ("model", "resolution", "steps"):
            if args.get(k):
                if p.get(k) != args[k]:
                    changes.append(f"{k}：{p.get(k) or '默认'} → {args[k]}")
                p[k] = args[k]
        state["auto_params"] = p
        return changes

    def handle_agent_turn(self, text: str) -> Optional[dict]:
        """agent 主导的一轮：LLM 出动作计划 → 校验 → 按序执行现有执行器。

        返回 None 表示 agent 不可用/无法解析（上层回退关键词路径，保证不断流）。
        """
        pl = agent.plan(text, self.current_project)
        if not pl:
            return None
        return self._execute_plan(pl, text)

    def handle_agent_turn_stream(self, text: str):
        """流式版 agent 轮次（2026-09-22）。

        逐块 yield 生成进度，供 UI 实时显示：
          {"reply": <累积正文>, "thinking_chars": N}   —— 推理/正文生成中
          {"result": <同 handle_agent_turn 的返回值>}   —— 最后一块（终止）
        plan 为 None 时 result 也是 None，上层照旧回退关键词路径。
        """
        plan = None

        for ev in agent.plan_stream(text, self.current_project):
            if "plan" in ev:
                plan = ev["plan"]
                break
            yield ev
        yield {"result": self._execute_plan(plan, text) if plan else None}

    def _execute_plan(self, pl: dict, text: str) -> dict:
        """按序执行计划里的动作，并把各步结果拼成一条回复。"""
        st = self.current_project
        # 上一组建完了吗？是的话在这一轮决定轮转（用户要求：建模后先能在对话框里看到模型，
        # 下次发送再新建对话组）。若已轮转，去掉计划里的 new_conversation —— 否则会建两个组。
        names0 = [a["name"] for a in pl["actions"]]
        # ★ 2026-10-18：不再只在上组建完时调用 —— 没做完时也要判断"是不是换了东西"，
        #   否则新需求会被接进旧组（实测：说"做手机支架"被接进"六边形笔筒"那组）
        before = (st or {}).get("folder")
        st_new = self.rotate_if_finished(names0, user_text=text)
        if st_new is not st:
            st = st_new
            self.current_project = st
            if (st or {}).get("folder") != before:
                pl["actions"] = [a for a in pl["actions"] if a["name"] != "new_conversation"]
        # 注：显存让渡**不在这里**做。这里提前卸会踩坑：紧随其后的"提示词扩写"要调对话模型，
        # 又把它懒加载回 11GB，正好卡在生成前。正确的顺序在咽喉点里：
        # 扩写（要模型）→ 让出显存 → ComfyUI 生成 → 后台回热（见 _free_tuned_for_gpu_work）
        reply_parts = [pl["reply"]] if pl["reply"] else []
        images, last = [], None
        acting = False
        # 这一轮是不是"在描述要做的东西"（决定要不要自动出图）
        # 只认 set_requirement：纯咨询(answer)、问问题、改参数都不算，
        # 否则用户聊两句就烧一次 GPU。
        _want_auto_image = False

        for act in pl["actions"]:
            name, a = act["name"], act["args"]
            if name == "answer":
                continue
            acting = True

            if name == "set_requirement":
                txt = a.get("text") or text
                # ★ 2026-10-19：模型改写需求时**不许动用户给的数字**。
                #   实测 qwen3.8 把「壁厚3mm，高100mm」写成「壁厚100mm，高100mm」，
                #   后面出图和 3D 全按 100mm 壁厚走。数字是硬约束，以用户原文为准。
                if text and txt != text and not _numbers_preserved(text, txt):
                    print(f"[DSH] 需求数字被模型改写，回退用户原文: {txt!r} → {text!r}", flush=True)
                    txt = text
                _want_auto_image = True          # 在描述要做的东西 → 待会儿自动出图
                if _is_requirement_followup(st):
                    # 同上：补充/修改也不许模型改用户数字（实测有 3mm→100mm 的前科）
                    if text and txt != text and not _numbers_preserved(text, txt):
                        print(f"[DSH] 补充需求数字被改写，回退用户原文: {txt!r} → {text!r}", flush=True)
                        txt = text
                    changes = _apply_refinement(st, txt)
                    st.setdefault("req_notes", []).append(txt)
                    if changes:
                        reply_parts.append("已更新：" + "；".join(changes))
                else:
                    resolved = ic.resolve_entity(txt)
                    st["base_prompt"] = resolved
                    st["refinements"] = []
                    st["req_notes"] = [resolved]
                    st["user_prompt"] = resolved
                    st["understood"] = resolved
                    size_mm = _extract_size_mm(resolved) or 120
                    st["auto_params"] = {
                        "style": _auto_style(resolved), "size_mm": size_mm,
                        "color": _extract_color_explicit(resolved) or "未指定（可后续补充）",
                        "model": _auto_model(resolved), "resolution": _HD_RES, "steps": "8步",
                    }
                    st["stl_size"] = size_mm
                    if st.get("phase") in (None, "empty"):
                        st["phase"] = "confirm"
                pm.save_project(st)

            elif name == "set_param":
                ch = self._apply_param_values(st, a)
                if ch:
                    reply_parts.append("已调整：" + "；".join(ch))
                pm.save_project(st)

            elif name == "generate_image":
                # 2026-10-21：需求对话结束后**不自动出图**，必须用户点按钮或明确说"生成图片"。
                if not any(k in (text or "") for k in
                           ("生成图片", "出图", "生图", "画一张", "生成一张", "改图", "修改图片")):
                    reply_parts.append("需求已记录。确认后点「🎨 生成图片」，或直接说「生成图片」。")
                    continue
                last = self._do_generate_images(st)
                images += last.get("images", []) or []
                if last.get("reply"):
                    reply_parts.append(last["reply"])

            elif name == "enhance_image":
                if st.get("selected"):
                    self._enhance_selected(st)
                    pm.save_project(st)

            elif name == "modify_image":
                if a.get("note"):
                    _apply_refinement(st, a["note"])
                    st.setdefault("req_notes", []).append(a["note"])
                if not st.get("candidates"):
                    last = self._do_generate_images(st)      # 还没有图 → 直接出图
                else:
                    last = self._do_generate_images(st)
                images += last.get("images", []) or []
                if last.get("reply"):
                    reply_parts.append(last["reply"])

            elif name == "generate_3d":
                # 2026-10-21：只有用户明确说建模/点按钮才建模，需求对话不自动跑 3D。
                if not any(k in (text or "") for k in
                           ("建模", "生成模型", "开始建", "做3D", "3D建模", "生成3D")):
                    reply_parts.append("需求已记录。确认后点「🧊 生成建模」。")
                    continue
                # 还没有图 → 先出图再建模（用户说"开始建模"时通常就是这个意思）
                if not st.get("selected") or not Path(st.get("selected", "")).exists():
                    if not st.get("candidates"):
                        gi = self._do_generate_images(st)
                        images += gi.get("images", []) or []
                        if gi.get("reply"):
                            reply_parts.append(gi["reply"])
                    if not st.get("selected") and st.get("candidates"):
                        self._do_select_image(st, 0)
                if a.get("model"):
                    st["auto_params"]["model_3d"] = a["model"]
                last = self._do_generate_3d(st)
                images += last.get("images", []) or []
                if last.get("reply"):
                    reply_parts.append(last["reply"])

            elif name == "export_stl":
                if not st.get("glb_path"):
                    reply_parts.append("还没有 3D 模型，先建模才能导 STL。")
                else:
                    if a.get("size_mm"):
                        st["stl_size"] = float(a["size_mm"])
                    last = self._do_export_stl(st)
                    if last.get("reply"):
                        reply_parts.append(last["reply"])

            elif name == "new_conversation":
                last = {"project": self.new_project(), "reply": "", "images": []}
                reply_parts.append("已新建对话组，可以开始下一个了。")

            elif name == "switch_conversation":
                if a.get("folder"):
                    last = {"project": self.switch_project(a["folder"]), "reply": "",
                            "images": []}

            elif name == "open_bambu":
                if not st.get("stl_path"):
                    reply_parts.append("还没有 STL，先建模并导出。")
                else:
                    last = self._do_open_bambu(st)
                    if last.get("reply"):
                        reply_parts.append(last["reply"])

        # ★★ 2026-10-18：说完需求自动出图（回到旧版体验）★★
        #
        # 宇凯反馈："对话之后没有生图以及生成建模"。
        # 实测确认：只说一句需求 → images=[] ，等 8 秒也只有确认文案，
        # 用户必须手动点「生成图片」才有图 ✗
        #
        # 旧版体验是"说完就出图"，用户要的就是这个。
        # 触发条件卡得比较紧，避免每次都白烧 GPU：
        #   ① 这一轮确实记录了需求（有 set_requirement）
        #   ② 目前还没有任何图
        #   ③ 模型自己没调 generate_image（它调了就不重复）
        #   ④ 不是"继续改现有模型"的场景（没有 glb/stl）
        #   ⑤ 用户这句话不是在问问题/聊方案（是明确要做东西）
        # 出图后 _do_generate_images 内部会 arm_autopilot → 5 秒后自动建模，
        # 这样"说一句 → 看到模型"整条链路就自动走完了。
        # ★ 2026-10-21 宇凯要求：对话结束**不自动出图**，必须由用户点「🎨 生成图片」。
        #   保留下面这段历史实现作为记录，但关闭自动触发；否则每轮需求都白烧一次 GPU。
        if False and (_want_auto_image
                and not images
                and not st.get("candidates")
                and not st.get("glb_path")
                and not st.get("stl_path")
                and "generate_image" not in names0
                and "modify_image" not in names0
                and "generate_3d" not in names0):
            try:
                _gi = self._do_generate_images(st)
                images += _gi.get("images", []) or []
                if _gi.get("reply"):
                    reply_parts.append(_gi["reply"])
                st = _gi.get("project") or st
            except Exception as _e:
                print(f"[DSH] ⚠️ 自动出图失败（不影响对话）: {_e!r}")

        # 后置：执行器可能已把 state 换成新项目（建模完自动新建组）
        final_state = (last or {}).get("project") or st
        reply = "\n\n".join(p for p in reply_parts if p).strip()
        if not reply:
            reply = "好的。" if not acting else "已执行。"
        self._append_conversation(final_state, "user", text, reply, images=images)
        return {
            "project": final_state,
            "reply": reply,
            "images": images,
            "understanding": pl["understanding"],
            # 模型真实的推理结论（UI 展示"🧠 思考"，让思考深度可见）
            "thinking": pl.get("thinking", ""),
            "actions": [{"name": a["name"]} for a in pl["actions"]],
            "via": "agent",
        }

    def _strip_erasing_words(self, zh: str) -> str:
        """把需求里"抹掉识别特征"的片段剔掉，只留可执行的部分。

        ★ 什么时候用：用户在特征冲突里选了 A（保留特征）。
          这时要把「头部简约」「去掉叶冠」这类要求删掉，
          否则生成时又把特征抹平了 —— 白问一次 ✗

        ★ 只删**抹除类**分句，保留颜色/尺寸/风格：
            "菠萝，黄色卡通，去掉四肢，头部简约"
            → "菠萝，黄色卡通"
        """
        if not zh:
            return zh
        import re as _re
        parts = [p.strip() for p in _re.split(r"[，,；;]", zh) if p.strip()]
        keep = []
        for p in parts:
            if _re.search(r"(去掉|不要|去除|删掉|简化|简约|极简|省略|光滑|无|没有)", p):
                continue          # 抹除类分句 → 丢弃
            keep.append(p)
        return "，".join(keep) if keep else zh

    def handle_user_input(self, text: str) -> dict:
        """
        处理用户输入，返回更新后的项目状态 + AI 回复

        2026-09-22：**agent 优先** —— 先让 LLM 决定这一轮要做什么动作并执行；
        只有 agent 不可用/无法解析时才回退到下面的关键词+状态机路径。
        """
        # 用户一发言就取消「自动继续」—— 他显然还有自己的要求要说（宇凯要的就是这个语义：
        # 5 秒内没动静才替他动手，一说话就必须让位）
        st0 = self.current_project
        if st0:
            self.cancel_autopilot(st0, "用户发言")

        # ══════════════════════════════════════════════════════════════
        # ★ 识别特征冲突的 A/B 应答（2026-10-17）
        # ══════════════════════════════════════════════════════════════
        #
        # 生图前如果发现"需求会抹掉物体识别特征"，会先问用户二选一：
        #   A. 保留特征（能认出来）
        #   B. 坚持简化（可能认不出）
        #
        # ★ 为什么必须在 **agent 之前**处理：
        #   agent 会把用户回的"A"/"B"当成一句新的需求去理解，
        #   甚至可能重新走一遍生成流程 —— 那就白问了 ✗
        #   在这里先截住，语义明确、也不会重复问 ✓

        if agent_enabled():
            try:
                r = self.handle_agent_turn(text)
                if r:
                    return r
                print("[DSH] agent 未给出计划，回退关键词路径")
            except Exception as e:
                print(f"[DSH] ⚠️ agent 执行异常，回退关键词路径: {e!r}")

        # 上一组建完了 → 兜底路径没法判断"是继续改还是做新的"，按老行为轮转
        # ★ 2026-10-18：同样改成无条件调用，交给 rotate_if_finished 内部判断
        self.current_project = self.rotate_if_finished(None, user_text=text)

        state = self.current_project
        phase = state.get("phase", "empty")
        intent, arg = ic.classify(text, phase)

        reply = ""
        new_phase = phase

        # ── 阶段0：需求确认（聊天式累积：一直能聊，每轮给总结）──
        if phase in ("empty", "confirm", "refining"):
            if intent == ic.Intent.DESCRIBE_OBJECT or intent == ic.Intent.REFINE:
                # 首轮 = 定需求；后续轮 = 补充/修改 → 一律累积，不覆盖
                followup = _is_requirement_followup(state)
                resolved = ic.resolve_entity(text)

                if followup:
                    changes = _apply_refinement(state, text)
                    state.setdefault("req_notes", []).append(text)
                else:
                    changes = []
                    state["base_prompt"] = resolved      # 原始需求（细化时保留）
                    state["refinements"] = []
                    state["req_notes"] = [resolved]
                    state["user_prompt"] = resolved
                    state["understood"] = resolved
                    style = _auto_style(resolved)
                    size_mm = _extract_size_mm(resolved) or 120
                    state["auto_params"] = {
                        "style": style,
                        "size_mm": size_mm,
                        "color": _extract_color_explicit(resolved) or "未指定（可后续补充）",
                        "model": _auto_model(resolved),
                        "resolution": _HD_RES,
                        "steps": "8步",
                    }
                    state["stl_size"] = size_mm

                auto_params = dict(state["auto_params"])
                auto_params["notes"] = list(state.get("req_notes", [resolved]))
                auto_params["requirement"] = _build_prompt(state)
                auto_params["changes"] = changes

                # 每轮都给一份「综合总结」，而不是只确认一次
                reply = describer.generate_confirm_response(
                    user_input=resolved,
                    understood=state["understood"],
                    auto_params=auto_params,
                )
                new_phase = "confirm"

            elif intent == ic.Intent.REFINE_REQUIREMENT:
                # 进入"细化中"状态，提示用户继续输入（不调 LLM，秒回）
                reply = "请继续输入，告诉我你想细化/调整的要求。\n比如：「颜色改成蓝色」「尺寸改成 80mm」「加一个底座」"
                new_phase = "refining"

            elif intent == ic.Intent.START_GENERATE:
                # 直接开始生成
                return self._do_generate_images(state)

            elif intent == ic.Intent.NEW_PROJECT:
                return self._do_new_project()

            else:
                reply = "请先告诉我你想打印什么，比如：\n「我要一个熊大」\n「帮我设计一个机械手臂底座」\n「画一个茶杯垫」"

        # 注：原有的「阶段0.5 细化中」独立分支已删除（2026-09-22）——
        # refining 现已并入上面的需求聊天块，所有轮次统一走「累积 + LLM 总结」，
        # 那段"已按你的要求更新参数"的本地回复不会再被触发，避免两套逻辑并存。

        # ── 阶段1：生成图片 ──────────────────────────────
        elif phase == "generating":
            if intent == ic.Intent.REGENERATE:
                return self._do_generate_images(state)
            elif intent == ic.Intent.SELECT_IMAGE:
                idx = int(arg) if arg else 0
                return self._do_select_image(state, idx)
            elif intent == ic.Intent.REFINE:
                reply = "请描述你想怎么修改这张图，比如「颜色再亮一点」「背景换成海边」..."
                new_phase = "editing"
            elif intent == ic.Intent.NEW_PROJECT:
                return self._do_new_project()
            elif intent == ic.Intent.DESCRIBE_OBJECT:
                # 用户重新描述需求（或脏状态：generating 但无候选图）
                resolved = ic.resolve_entity(text)
                state["user_prompt"] = resolved
                state["understood"] = resolved
                style = _auto_style(resolved)
                size_mm = _extract_size_mm(resolved) or 120
                state["auto_params"] = {"style": style, "size_mm": size_mm,
                                        "color": "未指定", "model": _auto_model(resolved),
                                        "resolution": "1024×1024", "steps": "28步"}
                state["stl_size"] = size_mm
                reply = describer.generate_confirm_response(
                    user_input=resolved, understood=resolved, auto_params=state["auto_params"])
                new_phase = "confirm"
            else:
                # 无候选图时兜底：回到需求确认，避免 cands[0] 越界
                if not state.get("candidates"):
                    resolved = ic.resolve_entity(text)
                    state["user_prompt"] = resolved
                    state["understood"] = resolved
                    style = _auto_style(resolved)
                    size_mm = _extract_size_mm(resolved) or 120
                    state["auto_params"] = {"style": style, "size_mm": size_mm,
                                            "color": "未指定", "model": _auto_model(resolved),
                                            "resolution": "1024×1024", "steps": "28步"}
                    state["stl_size"] = size_mm
                    reply = describer.generate_confirm_response(
                        user_input=resolved, understood=resolved, auto_params=state["auto_params"])
                    new_phase = "confirm"
                else:
                    return self._do_select_image(state, 0)

        # ── 阶段2：选图 ─────────────────────────────────
        elif phase == "selecting":
            if intent == ic.Intent.START_3D:
                return self._do_generate_3d(state)
            elif intent == ic.Intent.EXPORT_STL:
                return self._do_export_stl(state)
            elif intent == ic.Intent.OPEN_BAMBU:
                return self._do_open_bambu(state)
            elif intent == ic.Intent.REFINE:
                # 若这句话本身带具体修改 → 立刻改图；纯抱怨则进"修改中"问清要改什么
                pure_complaint = any(c in text for c in _COMPLAINT_WORDS) and \
                                 not any(a in text for a in _ACTIONABLE_WORDS)
                if not pure_complaint:
                    changes = _apply_refinement(state, text)
                    tip = ("\n\n已实际应用到生成参数：" + "；".join(changes)) if changes \
                        else "\n\n（颜色/尺寸/风格未变，已按这句文字要求重新生成）"
                    reply = f"好的，按你的要求「{text}」重新生成图片，请稍候…{tip}"
                    self._append_conversation(state, "user", text, reply)
                    return self._do_generate_images(state)
                new_phase = "editing"
                reply = ("请描述你想怎么改，比如「颜色换成蓝色」「背景简单一点」「小一点」「不要底座」。\n"
                         "说得越具体，改得越准。")
            elif intent == ic.Intent.REGENERATE:
                return self._do_generate_images(state)
            elif intent == ic.Intent.NEW_PROJECT:
                return self._do_new_project()
            elif intent == ic.Intent.SELECT_IMAGE:
                return self._do_select_image(state, int(arg) if arg else 0)
            elif intent == ic.Intent.DESCRIBE_OBJECT:
                # 用户给出「新的需求描述」→ 当新需求重新确认，绝不静默跑 3D
                resolved = ic.resolve_entity(text)
                state["base_prompt"] = resolved
                state["refinements"] = []
                state["user_prompt"] = resolved
                state["understood"] = resolved
                style = _auto_style(resolved)
                size_mm = _extract_size_mm(resolved) or 120
                state["auto_params"] = {
                    "style": style, "size_mm": size_mm,
                    "color": _extract_color_explicit(resolved) or "未指定（可后续补充）",
                    "model": _auto_model(resolved),
                    "resolution": "1024×1024", "steps": "8步",
                }
                state["stl_size"] = size_mm
                reply = describer.generate_confirm_response(
                    user_input=resolved, understood=resolved, auto_params=state["auto_params"])
                new_phase = "confirm"
            else:
                # 兜底：给明确指引，不要默认跑 3D（曾导致输入提示词就弹「建模已完成」）
                reply = (
                    "已有一张待建模的图片。\n\n"
                    "• 要开始 3D 建模：点「🧊 生成建模」或说「开始建模」\n"
                    "• 想换需求重新做：点「🏠 新建对话组」，或直接描述你想要的新物体"
                )

        # ── 阶段2.5：修改图片 ─────────────────────────────
        elif phase == "editing":
            if intent == ic.Intent.START_3D:
                return self._do_generate_3d(state)
            elif intent == ic.Intent.REGENERATE:
                return self._do_generate_images(state)
            elif intent == ic.Intent.NEW_PROJECT:
                return self._do_new_project()
            else:
                # 修改描述（含 REFINE）：**真正落到 auto_params + refinements** 再重生成。
                # 纯抱怨（图片有问题/不好看…）没有可执行信息 → 先问清要改什么，别白烧一次生图。
                pure_complaint = any(c in text for c in _COMPLAINT_WORDS) and \
                                 not any(a in text for a in _ACTIONABLE_WORDS)
                if pure_complaint:
                    new_phase = "editing"
                    reply = ("收到，我来改。请说得具体一点，比如「颜色换成红色」"
                             "「背景简洁一点」「小一点」「去掉底座」，我按你说的重新生成。")
                    self._append_conversation(state, "user", text, reply)
                    return {"project": state, "reply": reply, "images": []}
                changes = _apply_refinement(state, text)
                tip = ("\n\n已实际应用到生成参数：" + "；".join(changes)) if changes \
                    else "\n\n（颜色/尺寸/风格未变，已按这句文字要求重新生成）"
                reply = f"好的，按你的要求「{text}」重新生成图片，请稍候…{tip}"
                self._append_conversation(state, "user", text, reply)
                return self._do_generate_images(state)

        # ── 阶段3：3D建模 ────────────────────────────────
        elif phase == "3d":
            if intent == ic.Intent.EXPORT_STL:
                return self._do_export_stl(state)
            elif intent == ic.Intent.OPEN_BAMBU:
                return self._do_open_bambu(state)
            elif intent == ic.Intent.REGENERATE:
                return self._do_generate_3d(state)
            elif intent == ic.Intent.REFINE:
                reply = "请描述你想怎么调整这个 3D 模型..."
            elif intent == ic.Intent.NEW_PROJECT:
                return self._do_new_project()
            else:
                return self._do_export_stl(state)

        # ── 阶段4：STL ─────────────────────────────────
        elif phase == "stl":
            if intent == ic.Intent.OPEN_BAMBU:
                return self._do_open_bambu(state)
            elif intent == ic.Intent.REFINE:
                size_mm = _extract_size_mm(arg) or state.get("stl_size", 120)
                state["stl_size"] = size_mm
                return self._do_export_stl(state)
            elif intent == ic.Intent.NEW_PROJECT:
                return self._do_new_project()
            else:
                reply = "你的 3D 模型已就绪！可以直接在 Bambu Studio 里打印，或者告诉我调整尺寸。"

        # ── 保存 ─────────────────────────────────────────
        state["phase"] = new_phase
        self._append_conversation(state, "user", text, reply)
        pm.save_project(state)

        return {
            "project": state,
            "reply": reply,
            "images": [],
        }

    # ─── 流式支持：需求描述拆成「非 LLM 部分」+「LLM 流式部分」+「收尾」───

    def prepare_describe(self, text: str):
        """需求聊天阶段的非 LLM 部分：把这一轮的话**累积**进需求，再交给上层流式总结。

        2026-09-22（宇凯要「聊天式定需求」）：
        一组对话里可以一直聊，每轮都是补充/修改，AI 每轮都给一份**综合总结**，
        用户看总结决定「生成图片」还是继续聊。原实现每轮都重置 base_prompt 并清空
        refinements —— 等于把前面说过的话全忘掉。
        返回 (state, resolved, params)，params 里带 notes/requirement 供 LLM 总结。
        """
        state = self.current_project
        resolved = ic.resolve_entity(text)
        followup = _is_requirement_followup(state)

        if followup:
            # 累积：走增量（refinements 进生成 prompt；颜色/尺寸/风格按 delta 更新）
            changes = _apply_refinement(state, text)
            state.setdefault("req_notes", []).append(text)
        else:
            changes = []
            state["base_prompt"] = resolved          # 首轮原始需求
            state["refinements"] = []
            state["req_notes"] = [resolved]
            state["user_prompt"] = resolved
            state["understood"] = resolved

            style = _auto_style(resolved)
            size_mm = _extract_size_mm(resolved) or 120
            state["auto_params"] = {
                "style": style,
                "size_mm": size_mm,
                "color": _extract_color_explicit(resolved) or "未指定（可后续补充）",
                "model": _auto_model(resolved),
                "resolution": _HD_RES,
                "steps": "8步",
            }
            state["stl_size"] = size_mm

        params = dict(state.get("auto_params", {}))
        params["notes"] = list(state.get("req_notes", [resolved]))   # 多轮上下文
        params["requirement"] = _build_prompt(state)                 # 累积后的完整需求
        params["followup"] = followup
        params["changes"] = changes        # 本轮实际改了什么（给 UI 展示"理解过程"）
        return state, resolved, params

    def finish_reply(self, state: dict, user_text: str, reply: str, phase: str = "confirm") -> dict:
        """流式结束后收尾：写 phase、落对话、存盘"""
        state["phase"] = phase
        self._append_conversation(state, "user", user_text, reply)
        pm.save_project(state)
        return {"project": state, "reply": reply, "images": []}

    # ─── 步骤执行 ────────────────────────────────────────────

    def _do_generate_images(self, state: dict) -> dict:
        """生成 1 张图 —— 两阶段优化的**第一阶段**：512 快速预览，秒出。

        2026-09-22 移植旧版优化（.backup_pre_v3「两阶段生成」2026-08-12）：
        预览用低精度快速出图，用户确认后再 _enhance_selected() 走 img2img
        高清重绘（保构图），最终喂给 3D 的是高清图。
        好处：细化迭代循环快得多，而最终质量不降。
        """
        state["phase"] = "generating"
        pm.save_project(state)

        # ══════════════════════════════════════════════════════════════
        # ★ 识别特征冲突 → **静默修正**（2026-10-17 宇凯定）
        # ══════════════════════════════════════════════════════════════
        #
        # 背景：菠萝案例追到的根因是「需求把物体的识别特征抹掉了」
        #   （菠萝靠叶冠/菱形纹理识别，"头部简约"要求抹平它们）
        #   结果出纯色剪影 → 3D 重建出一块板 → 白烧两次 GPU。
        #
        # ★ 第一版做成了弹窗问 A/B，宇凯明确否掉：
        #   "不要拦截机制" —— 他不接受打断流程、让用户打字选。
        #
        # ★ 现在改成**不打扰的自动修正**：
        #   检测到会抹掉特征 → 直接把那些抹除要求从提示词里剔掉，
        #   照常出图。用户什么都不用做，拿到的就是"能认出来的那个东西" ✓
        #
        # ★ 为什么删要求而不是问：
        #   用户说"去掉四肢"通常想表达"别画成有手有脚的小人"，
        #   而不是"我要一块认不出来的板子"。他的真实目的是要个能用的模型，
        #   删掉一句自相矛盾的要求，比打断他问一轮更符合意图 ✓
        # ══════════════════════════════════════════════════════════════
        try:
            from .identity_guard import feature_erasure_conflict
            _zh_for_check = "，".join(
                [x for x in (
                    state.get("base_prompt") or state.get("user_prompt") or "",
                    *[r for r in (state.get("refinements") or []) if r],
                ) if x]
            )
            _conflict = feature_erasure_conflict(_zh_for_check)
            if _conflict:
                _before = state.get("base_prompt") or state.get("user_prompt") or ""
                _after = self._strip_erasing_words(_before)
                if _after and _after != _before:
                    state["base_prompt"] = _after
                    state["user_prompt"] = _after
                # 细化要求里也剔掉
                _drop = ("简约", "极简", "简化", "光滑", "省略", "去掉叶", "不要叶",
                         "去掉纹", "不要纹")
                state["refinements"] = [r for r in (state.get("refinements") or [])
                                        if r and not any(w in r for w in _drop)]
                pm.save_project(state)
                print(f"[DSH] 🔧 识别特征保护：{_conflict['subject']} 靠 "
                      f"{_conflict['features']} 识别，已自动去掉会抹掉它的要求"
                      f"（{'/'.join(_conflict['erases'])}）→ 按能认出来的样子生成")

        except Exception as _e:
            # 修正出错绝不能挡住正常生成
            print(f"[DSH] ⚠️ 识别特征修正异常（忽略，继续生图）: {_e!r}")

        from .pipeline import Pipeline
        pipe = Pipeline()
        params = state.get("auto_params", {})
        _ref = state.get("ref_upload") or ""
        if _ref:
            # 参考图分支（2026-09-23）：用最简中文 prompt 直送 Qwen-Image-2.1，不再走
            # "颜色/风格/尺寸注入"和"扩写+英译"——E2E 实测发现后者会把"挥手"动作词翻译
            # 丢掉、自动塞入"坐姿+12×8×6英寸+毛绒材质"等，结果出一只"毛绒玩具熊坐在地上"。
            # 参考图本身已经决定形象和材质，再用文字强加只会和它打架 ✗。
            base_prompt = state.get("base_prompt") or state.get("requirement") or state.get("user_prompt", "")
            refs = [r for r in (state.get("refinements") or []) if r]
            zh_prompt = "，".join([base_prompt] + refs) if (base_prompt or refs) else "保留参考图里的角色"
            state["gen_prompt"] = zh_prompt
            state["gen_prompt_zh"] = zh_prompt
            cand_paths = []
            _free_tuned_for_gpu_work("生图")
            try:
                stage_results = pipe.generate_images(
                    zh_prompt, count=1,
                    resolution=_HD_RES, quality_name="标准",
                    model_key="qwen21_ref", skip_cache=True,
                    reference_image=_ref or None,
                )
                for result in stage_results:
                    if result.success and result.output_path and Path(result.output_path).exists():
                        img_bytes = Path(result.output_path).read_bytes()
                        cand_paths.append(pm.save_candidate_image(Path(state["path"]), img_bytes, len(cand_paths)))
                    else:
                        print(f"[DSH] 参考图生图失败: success={result.success}")
            except Exception as e:
                print(f"[DSH] 参考图生图异常: {e!r}")
                state["phase"] = "confirm"; pm.save_project(state); _rewarm_tuned_async()
                return {"project": state, "reply": f"图片生成失败：{e}", "images": []}
            _rewarm_tuned_async()
            if not cand_paths:
                state["phase"] = "confirm"; pm.save_project(state)
                return {"project": state, "reply": "图片生成失败（未返回有效结果），请重试。", "images": []}
            state["candidates"] = cand_paths
            state["selected_idx"] = 0; state["selected"] = cand_paths[0]
            state["phase"] = "selecting"
            state["img_stage"] = "final"     # 一步到位，不做高清重绘
            try: pm.save_selected_image(Path(state["path"]), Path(cand_paths[0]).read_bytes())
            except Exception: pass
            pm.save_project(state)
            return {"project": state,
                    "reply": "✅ 参考图生成完毕（Qwen-Image-2.1）。点「🧊 生成建模」按这张图做 3D 摆件。",
                    "images": cand_paths}

        # —— 非参考图走原来的"基句+颜色/风格注入→扩写+英译"链路 ——
        base_prompt = _build_prompt(state)
        extra = []
        color = params.get("color")
        if color and "未指定" not in str(color):
            extra.append(f"{color}的配色")
        if params.get("style"):
            extra.append(f"{params['style']}风格")
        zh_prompt = base_prompt + ("，" + "，".join(extra) if extra else "")

        # ★★ 2026-10-15 加：需求为空时**拒绝生成**，绝不硬着头皮往下走 ★★
        #
        # 实测事故（用户报「要浣熊，图片是对的，但建模出来一个人」）：
        #   对话组漂移后，新组的 user_prompt / understood 都是空串
        #     → base_prompt = ""
        #     → enhance_image_prompt("") **立刻返回空串**
        #       ★ 注意：它返回空串时**既不抛异常也不打任何日志** ——
        #         调用方那句 `if en_prompt` 不成立、⚠️ 分支也不会走，
        #         所以日志里干干净净、一条线索都没有，极难排查。
        #     → 兜底拼出 "a high quality 3D figurine of , clean light background, ..."
        #       **主体位置是空的**
        #     → 模型只好自己随机编一个主体：卡通小男孩、红发女战士……每次都不一样
        #
        # 结论：这不是"模型不听话"，是**提示词里压根没有主体**。
        # 之前硬生成的后果比报错坏得多：用户花掉 GPU 时间、等几分钟，
        # 拿到一个完全无关的东西还不知道为什么。宁可明确拒绝、把话说清楚。
        if not base_prompt.strip():
            state["phase"] = "confirm"
            pm.save_project(state)
            return {
                "project": state,
                "reply": (
                    "这组对话还没有说要做什么，我先不出图 ——\n\n"
                    "空提示词会让模型**自己编一个主体**，出来的东西跟你想要的完全无关"
                    "（实测：要浣熊出卡通小男孩、要大象出女战士）。\n\n"
                    "先说一句你要做什么（比如「一只浣熊，写实风格」），再点「🎨 生成图片」。"
                ),
                "images": [],
            }

        # 扩写 + 英译（2026-09-22 补回旧版「扩写→翻译→生成」链路）：
        # SDXL/FLUX 对中文理解差，且 pipeline 的翻译模型(qwen3:0.6b)没装会被静默跳过，
        # 中文原句直喂 → 出图草率/主题漂移。用常驻对话模型扩写成详细英文提示词。
        # 2026-09-25 修：enhance 失败时不再回退中文原句（那是「陶土饼茶杯垫」的根因）；
        # describer 现在自带 IP 别名识别 + 安全模板 —— 几乎不可能再返回空串。
        en_prompt = ""
        try:
            en_prompt = describer.enhance_image_prompt(zh_prompt)
            if en_prompt:
                print(f"[DSH] ✨ 生图提示词扩写+英译: {len(zh_prompt)}→{len(en_prompt)} 字符")
        except Exception as e:
            print(f"[DSH] ⚠️ 扩写+英译失败: {e!r}")
        # enhance 失败时返回 "" 才回退原句 —— 现在 _safe_english_template 几乎不可能空
        prompt = en_prompt if en_prompt else f"a high quality 3D figurine of {base_prompt}, clean light background, ultra detailed, sharp focus"
        # 兜底：原 zh_prompt 不再喂给模型（中文短句 → 陶土饼那种偏移）。
        state["gen_prompt"] = prompt          # 高清重绘复用同一 prompt（保持构图一致）
        state["gen_prompt_zh"] = zh_prompt    # 展示/回溯用

        # ══════════════════════════════════════════════════════════════
        # ★ 主体丢失检查：认不出来就**如实说 + 要细节**，不硬画
        #   （宇凯 2026-10-16：「重点不是补上IP而是确认需求…未知的我们
        #     要实事求是的说不知道，然后根据用户的反馈以及细节描述来生成」）
        #
        #   ── 为什么放在这里（生成之前）──
        #     主体丢了还往下画 = 白烧几分钟 GPU 出一个无关的东西。
        #     在**动 GPU 之前**拦下，成本几乎为零 ✓
        #
        #   ── 实测依据 ──
        #     「原神里的钟离，岩王帝君造型」扩写成了
        #       "stylized cartoon character, ..."（主体整个消失）
        #     而输出看着"挺正常"，用户拿到个随机角色还不知道为什么 ✗
        #
        #   ── 为什么只拦"主体丢了"，不拦"认错角色"──
        #     认错（钢铁侠→Minion）已有 IP 表那条路治；
        #     而"主体消失"没有任何词可查，只能靠这个检查。
        #     误拦正常生成比漏拦更烦人，所以 subject_lost 判不准时
        #     一律放行（见它注释里的第 ④ 条）✓
        # ══════════════════════════════════════════════════════════════
        try:
            from .describer import subject_lost, describe_unknown_subject
            if subject_lost(base_prompt or zh_prompt, prompt):
                state["phase"] = "confirm"
                # 记下"等用户补充细节"，下一轮用户说话就是他在补描述
                state["awaiting_detail"] = True
                pm.save_project(state)
                print(f"[DSH] ⚠️ 主体丢失 → 不生成，改为要细节。zh={zh_prompt[:50]!r}")
                return {
                    "project": state,
                    "reply": describe_unknown_subject(zh_prompt),
                    "images": [],
                }
        except Exception as e:
            # 检查本身出错不能挡住正常生成（宁可画错也不要卡住）
            print(f"[DSH] ⚠️ 主体检查异常（忽略，继续生成）: {e!r}")

        cand_paths = []
        # 让显存：生图前必须把常驻对话模型（微调 11GB / Ollama 12.9GB）卸掉，
        # 否则 ComfyUI 只剩几 GB → 实测超时（这个调用点是按钮路径与 agent 路径的公共咽喉）
        _free_tuned_for_gpu_work("生图")
        try:
            # ★ 2026-10-15：候选张数改为配置驱动（原来写死 count=1）。
            #
            #   为什么不直接改成 3：站点侧（lib/foundry.ts）只取 cand_0 **一张**，
            #   多出的 2 张用户根本看不到，却要多烧 ~2 倍的 GPU 时间和显存。
            #   要做成"生成 3 张让用户挑"必须先在站点加「选图」UI —— 那是另一件事。
            #   所以这里留成配置开关：站点支持选图后把 preview_candidates 改成 3 即可。
            #   铸想自己的 Gradio 界面本来就支持多候选（见 select_candidate 的 selected_idx）。
            _n_cand = int(pipe.config.get("stages", {}).get("image", {})
                          .get("preview_candidates", 1) or 1)
            stage_results = pipe.generate_images(
                prompt, count=_n_cand,
                resolution=_PREVIEW_RES,
                quality_name="快速",           # 预览档：步数减半
                model_key=_pick_image_model(),
                skip_cache=True,              # 否则 ComfyUI 命中缓存 → "改了参数却没变"
            )
            cand_paths = []
            for result in stage_results:
                if result.success and result.output_path and Path(result.output_path).exists():
                    img_bytes = Path(result.output_path).read_bytes()
                    path_str = pm.save_candidate_image(Path(state["path"]), img_bytes, len(cand_paths))
                    cand_paths.append(path_str)
                else:
                    print(f"[DSH] 生图失败: success={result.success} path={result.output_path}")
        except Exception as e:
            print(f"[DSH] 生图异常: {e!r}")
            state["phase"] = "confirm"
            pm.save_project(state)
            _rewarm_tuned_async()          # 生成结束就把对话模型请回来（下一轮免重载）
            return {"project": state, "reply": f"图片生成失败：{e}", "images": []}
        _rewarm_tuned_async()              # 后台回热：下一轮对话不用干等 ~70s

        if not cand_paths:
            state["phase"] = "confirm"
            pm.save_project(state)
            return {"project": state, "reply": "图片生成失败（未返回有效结果），请重试。", "images": []}

        state["candidates"] = cand_paths
        # 单图模式：直接选中这张，进入 selecting（可修改/建模）
        state["selected_idx"] = 0
        state["selected"] = cand_paths[0]
        state["phase"] = "selecting"
        state["img_stage"] = "preview"      # 等 3D 前再高清重绘（两阶段优化）
        # 落盘 selected.jpg：自动选中原本只改内存，重启后 load_project 反推不到
        # selected.jpg → "选中图"丢失（2026-09-22 修）
        try:
            pm.save_selected_image(Path(state["path"]), Path(cand_paths[0]).read_bytes())
        except Exception as e:
            print(f"[DSH] 选中图落盘失败: {e}")
        pm.save_project(state)

        # 图片就绪 → 布防"自动继续"：delay 秒内用户没提修改需求就自动进建模
        # （见 arm_autopilot / autopilot_fire；用户任何介入都会取消）
        if self.arm_autopilot(state, "图片已生成"):
            print(f"[DSH] 自动继续已布防：{self.autopilot_cfg()['delay_sec']:.0f}s 后若无修改需求"
                  f" → 自动用 {self.autopilot_cfg()['model']} 建模")

        style = params.get("style", "写实")
        model_info = _pick_image_model()
        color_txt = params.get("color", "未指定")
        size_txt = params.get("size_mm", state.get("stl_size", 120))
        # 把实际使用的英文提示词亮出来（用户要能看出"到底细不细"）
        en_line = ""
        if en_prompt:
            shown = en_prompt if len(en_prompt) <= 340 else en_prompt[:340] + "…"
            en_line = (f"\n\n🔤 **实际喂给模型的英文提示词**（扩写+英译后 {len(en_prompt)} 字符）：\n"
                       f"> {shown}")
        # ★ 2026-10-15：显示**真实**用的模型/分辨率/步数/质量档。
        #
        #   原来这里写死 f"（{model_info} 8步 turbo）" —— 和实际毫无关系：
        #     · qwen21_ref 的配置是 20 步，不是 8 步
        #     · 预览档只跑 512×512，不是 1024
        #   用户正是拿这一行判断"到底细不细"，印错数字等于骗人（实测踩到过）。
        # 真实参数由 pipeline 通过 StageResult.metadata 带回来，这里只负责显示。
        # 用 getattr 取：显示层不该假定结果对象的形状（测试替身就只有 success/output_path）。
        real = next(
            (getattr(r, "metadata", None) for r in (stage_results or []) if getattr(r, "success", False)),
            None,
        ) or {}
        spec = " · ".join(
            str(x) for x in (
                real.get("model") or model_info,
                real.get("resolution"),
                f"{real['steps']}步" if real.get("steps") else None,
                f"{real.get('quality')}档" if real.get("quality") else None,
            ) if x
        )
        reply = (
            f"🎨 **图片生成完毕！**\n\n"
            f"风格：{style} ｜ 颜色：{color_txt} ｜ 预计打印尺寸：{size_txt:g}mm\n"
            f"（{spec}）"
            f"{en_line}\n\n"
            "满意的话点「🧊 生成建模」进入 3D，或点「🔧 修改图片」重新生成。"
        )

        # 气泡里显示用户的中文原话（英文提示词已在上面的回复中展示）
        self._append_conversation(state, "user", zh_prompt, reply, images=cand_paths)

        return {
            "project": state,
            "reply": reply,
            "images": cand_paths,
        }

    def _do_select_image(self, state: dict, idx: int) -> dict:
        """用户选了一张图"""
        cands = state.get("candidates", [])
        if not cands:
            # 无候选图（脏状态/生图失败）：回退到需求确认，提示重新生成
            state["phase"] = "confirm"
            pm.save_project(state)
            reply = "还没有候选图，请先点「🎨 生图」生成图片。"
            self._append_conversation(state, "user", f"选择图 {idx+1}", reply)
            return {"project": state, "reply": reply, "images": []}

        if idx < 0 or idx >= len(cands):
            idx = 0

        # 候选图文件可能已被删/移动：自动挑第一张存在的，全都不在则回退确认
        if not Path(cands[idx]).exists():
            alive = [i for i, p in enumerate(cands) if Path(p).exists()]
            if not alive:
                state["phase"] = "confirm"
                state["candidates"] = []
                pm.save_project(state)
                reply = "候选图文件已不存在，请点「🎨 生图」重新生成。"
                self._append_conversation(state, "user", f"选择图 {idx+1}", reply)
                return {"project": state, "reply": reply, "images": []}
            idx = alive[0]

        state["selected_idx"] = idx
        state["selected"] = cands[idx]

        # 保存选中图
        img_bytes = Path(cands[idx]).read_bytes()
        pm.save_selected_image(Path(state["path"]), img_bytes)

        state["phase"] = "selecting"
        pm.save_project(state)

        style = state.get("auto_params", {}).get("style", "写实")
        reply = (
            f"你选择了**图 {'ABC'[idx]}**（{style}风格）。\n\n"
            "接下来：点「修改图片」调整这张图，或点「生成建模」直接进入 3D 建模。"
        )

        self._append_conversation(state, "user", f"选择图 {idx+1}", reply, images=[cands[idx]])

        return {
            "project": state,
            "reply": reply,
            "images": [cands[idx]],
        }

    def _enhance_selected(self, state: dict, pipe=None) -> str:
        """两阶段优化的**第二阶段**：把选中的预览图 img2img 高清重绘（保构图）。

        移植自旧版（.backup_pre_v3 → pipe.enhance_image）。预览是 512 快速档，
        3D 前先升到 1024 标准档，最终模型质量不降。
        返回高清图路径；已是高清或失败则返回原图。
        """
        cur = state.get("selected", "")
        if not cur or not Path(cur).exists():
            return cur
        if state.get("img_stage") == "hd":
            return cur
        # A user-supplied reference is an input, not a generated preview.
        # Repainting it silently changes the subject before reconstruction.
        if state.get('ref_upload') == cur:
            return cur

        params = state.get("auto_params", {}) or {}
        prompt = state.get("gen_prompt") or _build_prompt(state)
        try:
            from .pipeline import Pipeline
            pipe = pipe or Pipeline()
            _free_tuned_for_gpu_work("高清重绘")     # img2img 也要 ComfyUI 显存
            hd = pipe.enhance_image(
                cur, prompt,
                model_key=_pick_image_model(),
                quality_name="标准",
                resolution=_HD_RES,
            )
            if getattr(self, '_interactive', True):
                _rewarm_tuned_async()
            if hd and Path(hd).exists():
                bytes_ = Path(hd).read_bytes()
                new_path = pm.save_candidate_image(Path(state["path"]), bytes_,
                                                   len(state.get("candidates", [])))
                state.setdefault("candidates", []).append(new_path)
                state["selected"] = new_path
                state["selected_idx"] = len(state["candidates"]) - 1
                state["img_stage"] = "hd"
                try:
                    pm.save_selected_image(Path(state["path"]), bytes_)
                except Exception:
                    pass
                print(f"[DSH] 高清重绘完成: {Path(new_path).name}")
                return new_path
            print("[DSH] 高清重绘失败，继续用预览图")
        except Exception as e:
            print(f"[DSH] 高清重绘异常: {e!r}，继续用预览图")
        return cur

    def _do_generate_3d(self, state: dict) -> dict:
        """生成3D模型"""
        selected_img = state.get("selected", "")
        if not selected_img or not Path(selected_img).exists():
            # 尝试用第一张候选图
            cands = state.get("candidates", [])
            if cands:
                selected_img = cands[0]
            else:
                return {"project": state, "reply": "没有选中图片，请先生成图片。", "images": []}

        # ★★ 2026-10-19 修「同一组被建模两次、最终没有 GLB」★★
        #
        # 现象（网站端实测）：调 model 接口后日志出现两次建模——
        #   第一次「快速档 octree=256」成功（40000 面，已导 STL），
        #   紧接着又提交「高质量档 octree=512」，第二次把第一次的成果覆盖掉，
        #   期间 glb_path 一直是 None → 网站取不到模型 ✗
        #
        # 根因：出图后 `arm_autopilot` 会布防"5 秒后自动建模"。
        #   网站/外部显式调 _do_generate_3d 时如果没先取消布防，
        #   定时器照样到点再跑一次 → 同组两次建模。
        #   （AGENTS.md 里记过这条："外部调 _do_generate_3d 前必先 cancel_autopilot"，
        #     但当时只写在外部调用方，函数自己没兜底，换个入口就又踩）
        #
        # 修法：进入建模就把自动继续取消掉 —— 既然已经有人在建了，不需要定时器再来一次。
        try:
            self.cancel_autopilot(state, why="已显式开始建模")
        except Exception:
            pass

        state["phase"] = "3d"
        pm.save_project(state)

        from .pipeline import Pipeline
        pipe = Pipeline()

        # 两阶段第二阶段：3D 前先把预览图升到高清（旧版同款优化）
        was_preview = state.get("img_stage") != "hd"
        selected_img = self._enhance_selected(state, pipe)
        pm.save_project(state)
        # 高清重绘是 img2img（denoise=0.5），成品与预览会略有差异 —— 明确告知用户
        hd_note = ("🔍 建模前已把预览图高清重绘到 1024（细节更足，与预览略有差异）\n"
                   if was_preview and state.get("img_stage") == "hd" else "")

        # 用 config 里的默认 3D 模型（triposplat：原生前馈管线，快），
        # 不再依赖 pipeline 写死的 hunyuan3d（Hunyuan 20步，要好几分钟）。
        td_key = pipe.config.get("stages", {}).get("3d", {}).get("default_model", "triposplat")
        # 旧版「一句话生成」流程同样会设置这个，pipeline 内部多处会读它
        try:
            pipe.state.selected_image_path = str(selected_img)
        except Exception:
            pass

        try:
            import time as _t
            _t0 = _t.time()
            _free_tuned_for_gpu_work("建模")         # 3D 是最吃显存的一步，务必先让出来
            # 精度档可由 state 覆盖（auto_params.quality_3d）—— 见 autopilot：
            # 自动继续走"快速"（octree 256），用户自己点「生成建模」走"高质量"（octree 512，细节更多）。
            # ★ 2026-10-18：默认从"标准"提到"高质量" —— 宇凯反馈精度太低，
            #   而旧版三档全是 512 等于没差；现在"标准"已降到 384，
            #   默认仍要落在 512 这档，否则会比他原来看到的更糊 ✗
            _quality = (state.get("auto_params") or {}).get("quality_3d") or "高质量"
            result = pipe.generate_3d(model_key=td_key, quality_name=_quality, image_path=selected_img)
            if getattr(self, '_interactive', True):
                _rewarm_tuned_async()
            _elapsed = _t.time() - _t0
            if result and result.success and result.output_path:
                img_bytes = Path(result.output_path).read_bytes()
                glb_path = pm.save_glb(Path(state["path"]), img_bytes)
                state["glb_path"] = glb_path
                state["phase"] = "stl"
                pm.save_project(state)

                # 自动导出 STL（建模完一条龙：GLB → STL）
                size = state.get("stl_size", 120)
                stl_note = ""
                try:
                    stl_result = pipe.export_stl(glb_path, target_size_mm=float(size),
                                                 optimize_preset="print_ready")
                    if stl_result and stl_result.success and stl_result.output_path:
                        stl_bytes = Path(stl_result.output_path).read_bytes()
                        stl_path = pm.save_stl(Path(state["path"]), stl_bytes)
                        state["stl_path"] = stl_path
                        stl_note = f"✅ STL 已自动导出（{size}mm，{len(stl_bytes)/1024/1024:.1f}MB）"
                    else:
                        stl_note = "⚠️ STL 自动导出失败，可在侧边栏手动导出"
                except Exception as e:
                    stl_note = f"⚠️ STL 自动导出失败：{e}"

                pm.save_project(state)

                # 3D 模型描述：qwen3.8 是文本模型看不到 GLB，用确定性文案
                #
                # ★ 2026-10-18 加「精度说明」——宇凯反馈"精度太低"，
                #   追查发现他不知道自己用的是哪一档、也不知道上限在哪。
                #   与其让他猜，不如直接把这次的真实参数报出来：
                #     · 用了几何分辨率（octree）—— 决定细节能不能被表达
                #     · 出来多少面 —— 决定文件多大、打印细不细
                #   这不是免责声明，是让"精度"变成可沟通的数字 ✓
                _fr_octree = {"快速": 256, "标准": 384, "高质量": 512,
                              "极致": 768, "最高精度": 1024}.get(_quality, 512)
                _fr_faces = 0
                try:
                    import trimesh as _tm
                    _fr_faces = len(_tm.load(glb_path, force="mesh").faces)
                except Exception:
                    pass
                _fr_bits = [f"几何分辨率 octree **{_fr_octree}**"]
                if _fr_faces:
                    _fr_bits.append(f"网格 **{_fr_faces:,} 面**")
                _fr_line = "、".join(_fr_bits)
                # 只在低档位时提醒天花板，高档位不啰嗦
                _fr_tip = ""
                if _fr_octree <= 384:
                    _fr_tip = ("\n> 💡 这档追求速度。需要更多细节可说「用高质量重做」，"
                               "会更慢（约 4–8 分钟）。\n")
                elif _fr_octree >= 512:
                    _fr_tip = ("\n> 💡 已是本机高精度档。若细节仍不理想，说明受"
                               "单图重建的体素分辨率限制，换更清晰的正视图通常比调档位有效。\n")

                reply = (
                    f"🧊 **3D建模完成！**\n\n"
                    f"已用 **{td_key}** 从你选中的图片重建出 3D 模型"
                    f"（耗时 {_elapsed:.0f}s；本次输出为形状网格）。\n\n"
                    f"本次精度：{_fr_line}\n"
                    f"当前 STL 尺寸：**{size}mm**\n"
                    f"{_fr_tip}"
                    f"{hd_note}"
                    f"{stl_note}\n\n"
                    "🎬 上方是模型的 4 视角预览图；页面下方「🧊 3D 模型」查看器可拖动旋转、滚轮缩放。\n\n"
                    "✅ 本组建完了，模型就在上方。下一条消息如果开始做新东西，会自动新建对话组；"
                    "如果继续改这个模型（改尺寸 / 导 STL / 送打印），就留在本组。"
                )

                # 渲染 4 视角预览图贴进对话（2026-09-22）：
                # 旧版有 gr.Model3D，但 triposplat 的 GLB 有 40~80MB，浏览器加载慢/易空白；
                # 而且建模成功会自动切到新对话组 → 组件被隐藏 → 用户"建模完看不到"。
                # 贴成 PNG 后与其它图片一致，历史滚动随时可见。
                preview_png = ""
                try:
                    from .render3d import render_mesh_preview
                    # 优先渲染 STL（已过 mesh_optimizer：水密单体，面少）——
                    # GLB 是 triposplat 原始输出，含大量飞面碎片，渲染出来看着像"破的"，
                    # 而 STL 才是用户真正要打印的东西。
                    src_mesh = state.get("stl_path") or glb_path
                    if not Path(src_mesh).exists():
                        src_mesh = glb_path
                    preview_png = render_mesh_preview(src_mesh)
                except Exception as e:
                    print(f"[DSH] 3D 预览渲染失败（不影响建模）: {e!r}")

                conv_images = [preview_png] if preview_png else [selected_img]
                # 本建模完成，但**不在这里立刻新建对话组**（用户反馈：建模完看不到模型、
                # 对话组却已经新建了）。立刻 new_project 会让本轮返回一个空白新组 →
                # 3D 查看器对着空组、对话框里也看不到刚做出来的东西。
                # 改为打标记，等**下一条消息**进来时再轮转（见 _rotate_if_finished）。
                self._append_conversation(state, "user", "生成3D建模", reply, images=conv_images)
                state["pending_rotate"] = True
                pm.save_project(state)

                # 后台预热生图模型：3D 前的 _free_vram 把生图模型也卸了，
                # 预热可让下一个建模的首次生图免去 ~20-35s 重载
                if getattr(self, '_interactive', True):
                    _warm_image_model_async(
                        state.get("auto_params", {}).get("model") or _pick_image_model()
                    )

                return {"project": state, "reply": reply, "images": conv_images,
                        "glb": glb_path, "preview": preview_png, "auto_new": False,
                        "rotate_pending": True}

            else:
                reply = "3D 建模未返回有效结果：" + (getattr(result, 'error', '') or "请重试。")
                state["phase"] = "selecting"
        except Exception as e:
            reply = f"3D 建模失败：{e}"
            state["phase"] = "selecting"

        pm.save_project(state)
        return {"project": state, "reply": reply, "images": [selected_img]}

    def _do_export_stl(self, state: dict) -> dict:
        """导出 STL"""
        glb_path = state.get("glb_path", "")
        if not glb_path or not Path(glb_path).exists():
            return {"project": state, "reply": "没有 3D 模型，请先生成 3D。", "images": []}

        state["phase"] = "stl"
        pm.save_project(state)

        from .pipeline import Pipeline
        pipe = Pipeline()
        size = state.get("stl_size", 120)

        try:
            result = pipe.export_stl(glb_path, target_size_mm=float(size),
                                     optimize_preset="print_ready")
            if result and result.success and result.output_path:
                stl_bytes = Path(result.output_path).read_bytes()
                stl_path = pm.save_stl(Path(state["path"]), stl_bytes)
                state["stl_path"] = stl_path
                pm.save_project(state)

                size_mb = len(stl_bytes) / (1024 * 1024)
                # 实测补充：把"文件在哪、多大、能不能打"说清 —— 只回一句"已导出"而
                # 不给路径，用户根本拿不到文件（2026-09-23 宇凯反馈"点了没转"的真身 ✗）
                dims = ""
                faces = 0
                try:
                    import trimesh as _tm
                    _m = _tm.load(stl_path, force="mesh")
                    _b = _m.extents
                    dims = f"{_b[0]:.1f} × {_b[1]:.1f} × {_b[2]:.1f} mm"
                    faces = len(_m.faces)
                except Exception:
                    pass
                reply = (
                    f"✅ **STL 已导出**（可以直接拿去切片打印）\n\n"
                    f"  • 文件：`{stl_path}`\n"
                    f"  • 大小：{size_mb:.2f} MB" + (f"｜{faces} 面" if faces else "") + "\n"
                    + (f"  • 实际外形：{dims}（最长边已缩放到 {size} mm）\n" if dims else
                       f"  • 打印尺寸：最长边 {size} mm\n")
                    + f"  • 底面已落 Z=0、水密单体，支撑/自适应层高校验见 Bambu\n\n"
                    "要在界面里下载，看进度条下方那个文件卡片；或直接说「打开Bambu Studio」。"
                )
                return {"project": state, "reply": reply, "images": [],
                        "stl_path": stl_path}
            else:
                reply = "STL 导出失败，请重试。"
        except Exception as e:
            reply = f"STL 导出失败：{e}"

        pm.save_project(state)
        return {"project": state, "reply": reply, "images": []}

    def _do_open_bambu(self, state: dict) -> dict:
        """打开 Bambu Studio"""
        from . import bambu_print
        stl_path = state.get("stl_path", "")
        ok, msg = bambu_print.open_in_bambu_studio(stl_path or None)
        reply = msg
        if ok:
            reply += "\n\n在 Bambu Studio 里确认参数后点「打印」即可。\n如需调整尺寸，告诉我「改为 XXX mm」。\n\n我们自动开启了：\n• 支撑（30°）\n• 自适应层高"
        self._append_conversation(state, "user", "打开Bambu Studio", reply)
        return {"project": state, "reply": reply, "images": []}

    # ── 自动继续（宇凯 2026-09-23 要求）───────────────────────────────────────
    #   场景：图片就绪后（自己生成的 / 从网上找来的参考图），用户 N 秒内没提修改需求
    #        → 自动进入建模，走**多视图**（multiview：单图先转 6 视角再重建，多视角一致
    #          所以更像），而不是单视图。
    #   原则：只在用户**真的没反应**时才替他动手 —— 发消息、打字、点任何按钮都取消。
    #   为什么写得这么小心：宇凯否决过"只补充细节就自动烧 GPU"那种抢跑，所以这里
    #       布防/取消/到期判定都要能单独测，且到期后还要复核阶段与图片是否还在。
    def autopilot_cfg(self) -> dict:
        """读 config.json → autopilot（这个类没有 self.config，照 agent_enabled 的读法）。

        注意别缓存：宇凯改完 config 要能立刻生效（测试里也会临时改配置）。
        """
        try:
            cfg = json.loads((Path(__file__).parent.parent / "config.json")
                             .read_text(encoding="utf-8")).get("autopilot", {}) or {}
        except Exception:
            cfg = {}
        return {
            "enabled": bool(cfg.get("enabled", False)),
            "delay_sec": float(cfg.get("delay_sec", 5) or 5),
            "model": str(cfg.get("model", "multiview") or "multiview"),
            "quality": str(cfg.get("quality", "快速") or "快速"),
        }

    def arm_autopilot(self, state: dict, reason: str = "") -> bool:
        """图片就绪后布防：N 秒没动静就自动建模。返回是否布防成功。"""
        cfg = self.autopilot_cfg()
        if not cfg["enabled"] or not state:
            return False
        state["auto_deadline"] = time.time() + cfg["delay_sec"]
        state["auto_model"] = cfg["model"]
        state["auto_reason"] = reason or "图片就绪"
        pm.save_project(state)
        return True

    def cancel_autopilot(self, state: dict, why: str = "用户介入", save: bool = True) -> bool:
        """用户一有动静就取消。打字（未提交）走 save=False，避免每次按键都落盘。"""
        if not state or not state.get("auto_deadline"):
            return False
        state["auto_deadline"] = 0
        state["auto_reason"] = why
        if save:
            pm.save_project(state)
        return True

    def autopilot_due(self, state: dict, now: Optional[float] = None) -> tuple:
        """到点了没？(该不该动手, 说明)。**无副作用**，便于直接测。"""
        if not state:
            return False, "无状态"
        # 配置随时可能被关掉（用户改 config.json / 临时关掉自动继续）→ 到点也要再查一次，
        # 否则"关掉之后仍然自动烧 GPU"（我的测试就是这么抓出这条漏洞的）
        if not self.autopilot_cfg()["enabled"]:
            return False, "自动继续已关闭（config.autopilot.enabled=false）"
        dl = float(state.get("auto_deadline") or 0)
        if not dl:
            return False, "未布防"
        now = time.time() if now is None else now
        if now < dl:
            return False, f"还有 {dl - now:.1f}s"
        if state.get("phase") not in ("selecting", "preview", "images", "confirm"):
            return False, f"阶段不适用（{state.get('phase')}）"
        if not (state.get("selected") or state.get("candidates")):
            return False, "没有可用图片"
        return True, f"到期（{state.get('auto_reason') or '图片就绪'}）"

    def autopilot_fire(self, state: dict) -> dict:
        """到期后替他按下「生成建模」（多视图），并清掉布防。"""
        due, why = self.autopilot_due(state)
        if not due:
            return {"project": state, "reply": "", "images": [], "autopilot": "skip", "why": why}
        _cfg = self.autopilot_cfg()
        model = state.get("auto_model") or _cfg["model"]
        state.setdefault("auto_params", {})["model_3d"] = model
        # 精度档：自动继续走"快速"（160s）——写死"标准"时实测要十几分钟，自动路径等不起
        state["auto_params"]["quality_3d"] = _cfg["quality"]
        state["auto_deadline"] = 0
        state["auto_fired"] = why
        pm.save_project(state)
        res = self._do_generate_3d(state)
        res["autopilot"] = "fired"
        res["why"] = why
        return res

    def rotate_if_finished(self, plan_actions=None, user_text: str = "") -> dict:
        """上一条把模型做完了（pending_rotate）？在**这一条**消息里决定要不要轮转。

        用户要求："建模后还要能在对话框里看到模型，应该等下一次发送再新建对话组"。
        所以轮转放在这里而不是建模完那一刻。又因为"继续改这个模型"很常见
        （改尺寸再导 STL、送打印），不能无脑轮转：

          · 计划里是**开始做新东西**（new_conversation / set_requirement）→ 轮转
          · 计划里是**继续用现有模型**（export_stl / open_bambu / generate_3d / modify_image）
            → 不轮转，留在本组
          · 关键词兜底路径（plan_actions=None）→ 按老行为轮转（它没法判断是否是延续）

        ★★ 2026-10-18 修「需求串组」真 bug ★★
        ------------------------------------------------------------
        现象（宇凯实测）：上一个组是做「六边形笔筒」的、还没出图，
          新用户进来直接说「我想做个手机支架」
          → 需求被接进笔筒那组 → 生成提示词变成
            "a six-sided pen holder, ... one built-in phone stand"
          → 用户拿到的是「带手机支架的笔筒」，完全不是他要的 ✗

        根因：旧逻辑只在 pending_rotate=True（即上一组建模完成）时才考虑轮转。
          而"上组没做完、用户转头要做别的东西"这种最常见的情况，
          pending_rotate 一直是 False → 直接 return，新需求被 set_requirement
          当成"补充"追加到旧组 ✗

        修法：**「该不该开新组」和「上一组有没有做完」是两件事**，拆开判断。
          旧组只要还没产出实质成果（没图、没模型），而用户这句话明显是
          另一个东西 → 就开新组。判据用"主体是否变了"，避免把
          "把底座做大一点"这类补充误判成新需求。
        """
        st = self.current_project or {}
        names = set(plan_actions or [])

        # ── 分支 A：上一组已完成 → 沿用原逻辑（补充进来的 pending_rotate）──
        if st.get("pending_rotate"):
            if names:
                continuation = names & {"export_stl", "open_bambu", "generate_3d", "modify_image"}
                starts_new = bool(names & {"new_conversation", "set_requirement"})
                if continuation and not starts_new:
                    st.pop("pending_rotate", None)          # 留在本组：把标记留着会每次都想轮转
                    pm.save_project(st)
                    return st
                if not starts_new:
                    return st                                # 纯问答等：不动
            return self._rotate_to_new(st)

        # ── 分支 B：上一组没做完 → 只在"用户换了东西"时才开新组 ──
        #
        # 之所以要判"是不是换了东西"，是因为「补充需求」必须留在本组累积，
        # 否则用户说"再加个防滑纹"就会被拆到新组，前面的需求全丢 ✗
        if not st or names & {"new_conversation"}:
            return self._rotate_to_new(st) if st else st

        if user_text and self._looks_like_new_subject(st, user_text):
            print(f"[DSH] 🔀 检测到换了东西（旧组未完成，无产出）→ 自动开新组")
            return self._rotate_to_new(st)

        return st

    def _rotate_to_new(self, st: dict) -> dict:
        """把当前组归档，建一个新的（内部复用，避免两处重复代码）"""
        try:
            pm.archive_project(st["folder"])
        except Exception:
            pass
        new = self.new_project()
        try:
            pm.save_project(new)
        except Exception:
            pass
        return new

    def _looks_like_new_subject(self, st: dict, user_text: str) -> bool:
        """用户这句话是不是在说**另一个东西**？

        ★ 2026-10-18 两次走错路，结论记这里，别再试第三次：
        ① 第一版用"修饰语词表"排除补充（改成/换成/大一点…）→
           中文语序太灵活，"直径改成90mm"里的"直径"不在表里就被判成新需求，
           13 个测试挂 12 个 ✗
        ② 第二版改用 ic.resolve_entity 比实体 → 实测发现这个函数
           基本是**原样返回**（'颜色换成红色，大一点' → 返回它自己），
           根本没做物体识别，靠它判断等于没判 → 12 个测试挂 ✗

        ★ 所以最终方案：**只在两种非常明确的信号下开新组**，其余一律不换。
          宁可"该开没开"（用户能看到需求叠着、自己点新建），
          也绝不"误开"（用户莫名其妙丢上下文，更糟）。

        明确信号只有两个：
          A. 用户用了"做/想做个/来一个/换成做" + 明显是**名词短语**（≥2 个汉字）
             并且这句话里**完全没有**当前组需求的任何词
          B. 当前组是空的（没有 base_prompt）—— 那种情况本来就该写进去，不算换
        """
        # 已有产出 → 一律不换（最安全）
        if st.get("glb_path") or st.get("stl_path"):
            return False
        if st.get("phase") in ("model", "stl", "done", "generating"):
            return False
        if (st.get("images") or st.get("candidates")):
            return False

        cur = (st.get("base_prompt") or "").strip()
        if not cur:
            return False

        txt = (user_text or "").strip()
        if len(txt) < 4:
            return False

        # 信号 A：明确的"我要做X"句式
        m = re.match(r"^(?:我)?(?:想|要|需要)?(?:做|来|弄|整)(?:一个|个|一款|一件)?\s*(.+)$", txt)
        if not m:
            return False
        obj = m.group(1).strip()
        # 目标必须够长，且不含当前组的任何词
        if len(re.sub(r"[^一-龥a-zA-Z]", "", obj)) < 2:
            return False
        cur_words = set(re.findall(r"[一-龥]{2,}", cur))
        obj_words = set(re.findall(r"[一-龥]{2,}", obj))
        if cur_words & obj_words:
            return False            # 有共同词 → 是同一个东西的补充
        return True

    def _do_new_project(self) -> dict:
        """新建对话组"""
        proj = self.new_project()
        reply = (
            "🆕 新对话组已创建。\n\n"
            "描述你想打印的物品 —— 我会把它从想法做到 3D 实体，再导出 STL。"
        )
        return {"project": proj, "reply": reply, "images": []}

    def _append_conversation(self, state: dict, role: str, content: str, ai_reply: str = "", images: list = None):
        entry = {
            "role": role,
            "content": content,
            "ai_reply": ai_reply,
            "images": images or [],
            "timestamp": time.time(),
        }
        state.setdefault("conversation", []).append(entry)

    def get_initial_message(self) -> str:
        """获取欢迎消息（2026-09-25 精简：去掉举例）"""
        return (
            "👋 你好！欢迎使用**铸想 ForgeForm**。\n\n"
            "描述你想打印的物品 —— 我会把它从想法做到 3D 实体，再导出 STL 给切片软件打印。"
        )
