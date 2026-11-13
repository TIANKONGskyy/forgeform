"""描述生成器：为每张图/3D模型生成详细中文描述（调用本地 LLM）

2026-09-22：对话模型改为 **config 驱动**（config.json → models.llm），不再硬编码。
背景：gpt-oss 这类推理模型即使 think=false 也会先生成思考 token，
会把 num_predict 预算吃光 → response/content 为空 → 上层走兜底模板。
所以这里统一按配置传 think/num_predict，并在"正文为空"时自动降档重试一次。
"""
import subprocess, threading, json, time, re
from pathlib import Path


# Ollama 地址（从 startup.py 保持一致）
OLLAMA_URL = "http://127.0.0.1:11434"

_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"

# config 缺失时的兜底（保守但可用：预算给足，避免推理模型饿死正文）
_DEFAULT_LLM = {
    "default_model": "qwen3.8:latest",
    "fallback_model": "qwen3.8:latest",
    "models": {},
    "temperature": 0.7,
    "default_num_predict": 1200,
}
# 正文为空时的"安全重试档"（对推理模型有效：降低思考量 + 放宽预算）
_SAFE_RETRY = {"think": "low", "num_predict": 1600}


def _load_llm_cfg() -> dict:
    try:
        with open(_CONFIG_PATH, encoding="utf-8") as f:
            cfg = (json.load(f).get("models", {}) or {}).get("llm", {}) or {}
        merged = dict(_DEFAULT_LLM)
        merged.update(cfg)
        return merged
    except Exception:
        return dict(_DEFAULT_LLM)


LLM_CFG = _load_llm_cfg()
OLLAMA_MODEL = LLM_CFG["default_model"]


def _profile(model: str = None) -> dict:
    """该模型的 think / num_predict / temperature（config.json → models.llm.models.<name>）

    2026-09-22：顶层 models.llm.think / default_num_predict 现在是**默认值**，
    单个模型在 models.llm.models.<name> 里的设置仍然优先。
    （原先顶层 think 会被忽略，改它没反应 —— 实际生效的只有 per-model 那一份。）
    """
    m = model or OLLAMA_MODEL
    p = {
        "think": LLM_CFG.get("think", False),
        "num_predict": int(LLM_CFG.get("default_num_predict", 1200)),
        "temperature": LLM_CFG.get("temperature", 0.7),
    }
    p.update((LLM_CFG.get("models") or {}).get(m, {}) or {})
    return p


def set_model(model: str) -> str:
    """切换对话模型（档位自动从 config 取）"""
    global OLLAMA_MODEL
    OLLAMA_MODEL = model
    return OLLAMA_MODEL


def llm_info() -> dict:
    """当前对话模型信息（给日志/UI 用）"""
    p = _profile()
    return {"model": OLLAMA_MODEL, "think": p["think"],
            "num_predict": p["num_predict"], "temperature": p["temperature"],
            "fallback": LLM_CFG.get("fallback_model", "")}


LAST_THINKING = ""   # 最近一次推理模型吐出的思考文本（think 档开启时 Ollama 会单独给）


def _generate_once(prompt: str, model: str, think, num_predict: int,
                   temperature: float, timeout: int, num_ctx: int = None,
                   json_mode: bool = False) -> str:
    """单次非流式调用，返回 response 文本（可能为空——推理模型吃光预算时）"""
    global LAST_THINKING
    import urllib.request
    opts = {"temperature": temperature, "num_predict": num_predict}
    # 2026-09-22：Agent 的 prompt 很长（工具说明 + 长示例 + 状态 + 历史），
    # 撞上 Ollama 默认上下文窗口会把输出从中间截断 → JSON 缺右括号 → 解析失败。
    # 显式放宽 num_ctx（KV 只占几百 MB）。
    if num_ctx:
        opts["num_ctx"] = int(num_ctx)
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/generate",
        data=json.dumps({
            "model": model,
            "prompt": prompt,
            "stream": False,
            "think": think,
            "keep_alive": -1,
            "options": opts,
            **({"format": "json"} if json_mode else {}),
        }).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        d = json.loads(resp.read())
    # 2026-09-22：think 开启时 Ollama 把推理过程放在独立的 thinking 字段里。
    # 以前只取 response，等于把模型的思考整段丢掉 → UI 上看不出它在权衡什么，
    # 用户感受就是"思考深度不够"。这里接出来供上层展示。
    LAST_THINKING = (d.get("thinking") or "").strip()
    return (d.get("response") or "").strip()


def _call_ollama(prompt: str, timeout: int = 60, json_mode: bool = False) -> str:
    """调用本地对话模型，返回文本响应（配置驱动 + 空正文自愈）。

    provider=strata → 走 OpenAI 兼容的 Strata（Qwen3.8-Flash-Next 125B）；
    否则仍走 Ollama 的三级降级。
    """
    if _training_lock_active():
        # 一旦真发请求，12.9GB 模型就会被拉回显存并常驻（keep_alive=-1），把训练拖慢 3 倍以上。
        # 返回非 JSON 文本 → agent 解析失败 → 优雅回退关键词路径（不崩、不抢显存）。
        print("[DSH] 微调训练进行中，拒绝加载对话模型（避免抢显存）")
        return "[训练进行中，对话模型暂时不可用]"
    if _agent_cfg_light().get("provider") == "strata":
        return _call_strata(prompt, timeout=timeout, json_mode=json_mode)
    p = _profile()
    try:
        out = _generate_once(prompt, OLLAMA_MODEL, p["think"], p["num_predict"],
                             p["temperature"], timeout, p.get("num_ctx"),
                             json_mode=json_mode)
    except Exception:
        # 冷启动重试一次（模型加载中）
        try:
            out = _generate_once(prompt, OLLAMA_MODEL, p["think"], p["num_predict"],
                                 p["temperature"], timeout * 2, p.get("num_ctx"),
                                 json_mode=json_mode)
        except Exception as e:
            return f"[描述生成失败: {e}]"
    if out:
        return out

    # 2) 空正文 → 安全档
    try:
        out = _generate_once(prompt, OLLAMA_MODEL, _SAFE_RETRY["think"],
                             _SAFE_RETRY["num_predict"], p["temperature"], timeout,
                             json_mode=json_mode)
        if out:
            print(f"[DSH] ⚠️ {OLLAMA_MODEL} 首轮正文为空，安全档重试成功")
            return out
    except Exception as e:
        print(f"[DSH] 安全档重试失败: {e}")

    # 3) 仍空 → 回退模型
    fb = LLM_CFG.get("fallback_model")
    if fb and fb != OLLAMA_MODEL:
        try:
            fp = _profile(fb)
            out = _generate_once(prompt, fb, fp["think"], fp["num_predict"],
                                 fp["temperature"], timeout, json_mode=json_mode)
            if out:
                print(f"[DSH] ⚠️ {OLLAMA_MODEL} 仍无正文，已回退 {fb}")
                return out
        except Exception as e:
            print(f"[DSH] 回退模型 {fb} 失败: {e}")

    return ""


# ── 生图提示词：扩写 + 英译（2026-09-22）────────────────────────────
# 背景：v3 改版把旧版的「扩写→翻译→生成」整条链丢了，中文原句直接喂 SDXL/FLUX，
# 加上 pipeline._ensure_english 的翻译模型(qwen3:0.6b)没装被静默跳过 → 出图差。
# 这里复用**常驻的对话模型**（config.json models.llm）做扩写+英译：
# 不额外加载模型、不占额外显存（keep_alive=-1 已驻留），且自带档位/自愈/回退。
_IMG_EN_CACHE: dict = {}

_IMG_PROMPT_SPEC = """You are a senior prompt engineer for text-to-image diffusion models (FLUX / Qwen-Image-2.1 / SDXL / Z-Image).

Turn the user's Chinese requirement into ONE detailed ENGLISH image prompt.

HARD RULES
- Output the prompt text ONLY. No explanation, no markdown, no quotes, no headings, no Chinese.
- English, comma-separated descriptive phrases (not full sentences).
- 40 to 90 words.
- Match style to the SUBJECT CATEGORY. Do NOT use a single product-photography template for everything:

  IF the request names a fictional / cartoon / IP CHARACTER (小黄人, 熊大, 哆啦A梦, 皮卡丘,
     马里奥, Hello Kitty, Tom and Jerry, Spider-Man, 动漫/游戏角色, 吉祥物, cartoon animals...):
    - KEEP THE ORIGINAL CARTOON DESIGN. NEVER turn it into a real living being: keep the
      canonical proportions, head-to-body ratio, eye style and facial features of the original.
    - Use the well-known ENGLISH IP NAME — always. NEVER a pinyin transliteration, and
      NEVER a generic noun. These are known mappings, use them:
        小黄人 / 迷你兵 / Minions   -> "Minion"
        熊大 -> "Briar Bear"   哆啦A梦 -> "Doraemon"   皮卡丘 -> "Pikachu"
        马里奥 -> "Mario"       蜘蛛侠 -> "Spider-Man"  hello kitty -> "Hello Kitty"
      Writing a generic word instead ("humanoid", "creature", "yellow figure", "cartoon man")
      is a FAILURE — it is exactly what makes the image model invent a realistic human.
    - "写实 / realistic" in the user's request means ONLY: it should look like a well-made
      PHYSICAL FIGURINE (clean surfaces, believable plastic / resin / matte material, nice
      studio lighting). It NEVER means real skin, pores, wrinkles, real human anatomy or a
      photoreal face. The user is asking for a "写实的手办", not a 真人.
    - FORBIDDEN words for such subjects: "natural skin texture", "detailed facial features",
      "photorealistic", "realistic human", "human anatomy", "photo portrait".
    - DO write instead: "stylized cartoon character, faithful to the original cartoon design,
      smooth simplified forms, collectible toy figurine, no realistic human anatomy, no skin pores".
    - Style: the same animation/film style as the IP, plus a physical collectible-figurine look.
    - Render as a figurine: "standing figurine, character portrait" is fine, a mild pose is OK —
      but do NOT add a scene.
    - Background: plain and uncluttered — pure white or very light seamless. NO scenery,
      NO room, NO floor, NO props, NO "environmental details", NO "subtle background".
      (This image feeds 3D reconstruction, so it must stay clean.)
    - Do NOT add "wearing a simple outfit / standing on a flat surface / product shot" etc.
    - Do NOT translate the character's name into pinyin.

  ELSE IF the request is industrial/mechanical (机械, 高达, robot arm base, gears, bracket, 摆件 for hardware):
    - Material: be specific (PLA 3D-printed / matte ABS plastic / brushed aluminium / walnut wood).
    - Style: industrial design, clean product render, minimalist.
    - Camera: 3/4 view, 45-degree, product shot.
    - Background: pure white (#FFFFFF), no floor, no shadow, no gradient, no surface.
      (阴影会被 3D 重建成一块底板 —— 实测 2026-10-19：带 soft contact shadow 的手机支架
       重建出 120×120mm 的碎片板，物体只剩中间一小块。)

  ELSE (home goods / daily items: 茶杯垫, mug, etc.):
    - Material MUST match the object's real-world default (cup coaster = cork / wood / fabric / silicone,
      NOT ABS plastic). Pick the most common real material.
    - Style: clean product render.
    - Camera: 3/4 view, slight perspective.
    - Background: light, simple, minimal.

  ★★★ IF the named character is NOT a well-known IP you can actually describe
      (e.g. a character from a game/anime you do not have reliable visual memory of,
       or a private/obscure character) — THIS IS THE MOST IMPORTANT CASE:

    - Do NOT fall back to a generic cartoon-character template. That makes the image
      model draw whatever stereotypical "cartoon character" it knows best
      (实测：写过 "stylized cartoon character, faithful to the original cartoon design"
       结果扩散模型画了一只 Minion —— 用户要的是原神钟离 ✗).
    - Do NOT pretend to know the exact canon. Skip all "faithful to the original" /
      "keep the canonical proportions" phrasing — you cannot be faithful to something
      you cannot picture.
    - INSTEAD: describe the character PURELY from what the USER said — body shape,
      colours, clothing, and any distinctive features they mentioned. Use only their
      words plus neutral wording. It is fine (and correct) for the prompt to be
      relatively short if the user gave little.
    - Translate literally but concretely: e.g. "岩王帝君造型, 棕色长发, 黑色长袍金色纹路"
      → "male character, long brown hair, black robe with golden patterns".
    - If the user gave almost no appearance detail, say so in the figure description
      with plain words (e.g. "a male humanoid character, plain dark robe") rather than
      borrowing another franchise's look.
    - NEVER substitute a different named character you do know.

- Always end with 1 quality booster: "ultra detailed, sharp focus".
- Do NOT invent extra items, text, logos or hands.
- The user's stated size, color and style are binding: never contradict them.

★★★ TWO HARD RULES that produced real failures on 2026-10-17 — follow them always:

  ① NEGATION DOES NOT WORK on diffusion models. Never write "no X" / "without X" /
     "remove X" as a negative instruction — the model reads the word X and DRAWS it.
     实测：「菠萝…去掉四肢」被扩写成 "no limbs"，出图照样长出了手脚 ✗

     INSTEAD, describe what the object IS, positively and completely.
     ★★ IMPORTANT — do not over-correct: keep EVERY iconic feature of the object.
        Removing the unwanted part must NOT turn it into a blank shape.
        实测踩到（2026-10-17）：「菠萝…去掉四肢」被改写成
          "smooth oval pineapple body, seamless closed silhouette, single solid volume"
          → 结果出图是**一块纯黄剪影**，菠萝的鳞片纹理/叶冠全没了 ✗
        正确写法是「去掉四肢，但**保留**菠萝该有的特征」：
          ✓ "a yellow cartoon pineapple with its diamond-pattern skin and leafy crown,
             closed rounded body, no separate arms or legs"
        规则：只去掉用户点名不要的部件，**其余原样保留**，
              并且务必把该物体最可识别的纹理/结构写出来（鳞片、叶冠、护目镜、
              罗纹、织纹…），否则模型会退化成纯色几何体。

  ② ALWAYS render exactly ONE object, centred, alone.
     Diffusion models love to draw pairs / sets / duplicates — and a 3D reconstruction
     then fuses them into one broken blob.
     实测：「带洞洞的拖鞋」出图是**两只鞋**，3D 重建把两只粘成了一个畸形整体 ✗

     So every prompt must describe a SINGLE subject:
       ✗ "slippers with holes"   (plural → draws two)
       ✓ "one single slipper with a perforated upper, standing alone"
     Use singular nouns, and prefer "a single …" / "one …" phrasing.
     Never use plural nouns for the subject, even if the user wrote plural —
     resolve it to one representative object (3D printing needs one solid part anyway).

For Chinese character names that have a canonical English name (e.g. 熊大 -> Briar Bear),
use the English name so the diffusion model recognises the IP.
If you are NOT confident the IP is well-known, follow the ★★★ rule above instead —
describing appearance from the user's words, without claiming canon accuracy."""


def _strip_think_tags(text: str) -> str:
    """去掉推理模型可能混进正文的  thinking…<｜end▁of▁thinking｜> / <thinking>…</thinking> 段"""
    if not text:
        return ""
    t = re.sub(r"<think(?:ing)?>.*?</think(?:ing)?>", " ", text, flags=re.S | re.I)
    return re.sub(r"</?think(?:ing)?>", " ", t, flags=re.I).strip()


# ══════════════════════════════════════════════════════════════════════
# 干净背景兜底（2026-11-13）
# ══════════════════════════════════════════════════════════════════════
#
# ★ 为什么需要
#   z_image 默认出"产品摄影"风格：灰底 + 地面接触阴影 + 反光。
#   2mini 会把这些**当成几何一起重建** —— 模型底下多一块带纹理的平板。
#   实测四件艺术品全中（输入图见 outputs/art/art_inputs.png）。
#
# ★ 为什么不用 "no shadow"
#   ① `_strip_negations` 会把 "no shadow" 整段删掉（shadows 在清理表里）
#   ② 扩散模型读到 shadow 反而更容易画阴影 —— 否定词是反效果
#   所以用**正向描述**：isolated on white / evenly lit / shadowless。
#
# ★ 只在缺的时候补
#   提示词里已经写了 white background / isolated 之类就不再重复。
_BG_MARKERS = (
    "white background", "isolated on", "seamless background",
    "plain background", "transparent background", "product photo on white",
)
_BG_SUFFIX = ", isolated on a pure white background, evenly lit, shadowless lighting"


def _ensure_clean_background(prompt: str) -> str:
    """给出图提示词补"干净背景"，减少后续 2mini 把背景重建进几何的概率。"""
    if not prompt:
        return prompt
    low = prompt.lower()
    if any(m in low for m in _BG_MARKERS):
        return prompt
    return prompt.rstrip(" ,.") + _BG_SUFFIX


def _clean_image_prompt(raw: str) -> str:
    """清掉 markdown/引号/中英夹杂与多余空白；中文占比过高视为失败返回空串"""
    if not raw:
        return ""
    t = raw.strip()
    # 去掉 think 段与代码块/围栏
    t = _strip_think_tags(t)
    t = re.sub(r"```[a-zA-Z]*|```", " ", t)
    # 去掉常见的模型自我描述前缀
    t = re.sub(r"^\s*(english\s+)?(image\s+)?prompt\s*[:：]\s*", "", t, flags=re.I)
    t = t.strip().strip('"').strip("'").strip()
    t = t.replace("\n", ", ")
    t = re.sub(r"\s*,\s*", ", ", t)
    t = re.sub(r"(?:,\s*){2,}", ", ", t)
    t = re.sub(r"\s{2,}", " ", t).strip(" ,.")
    if not t:
        return ""
    zh = len(re.findall(r"[\u4e00-\u9fff]", t))
    if zh and zh / max(len(t), 1) > 0.08:      # 还夹着中文 → 视为没翻译好
        return ""
    if len(t) < 30:                            # 太短 = 没扩写
        return ""
    return _ensure_clean_background(_strip_negations(t))[:900]


# ══════════════════════════════════════════════════════════════════════
# 否定词兜底清理（2026-10-17）
# ══════════════════════════════════════════════════════════════════════
#
# ★ 为什么光在 spec 里写规则不够
#   规则是"教 LLM 别写否定词"，但 LLM 不一定听 —— 实测就写过 "no limbs"。
#   而否定词在扩散模型上**是反效果**：读到 limbs 就画 limbs ✗
#   所以代码层再兜一层：直接把 "no X" / "without X" / "X-free" 这类片段删掉。
#
# ★ 为什么是"删掉"而不是"保留"
#   一个否定片段对生图**只有害无益**：
#     "pineapple, no limbs" → 模型看到 limbs
#     "pineapple"           → 模型画一个正常菠萝
#   删掉之后主体描述仍在，效果只会更好不会更差 ✓
#
# ★ 只删**明确的**否定短语，不动 "not sure"-这类正常句子
_NEG_PATTERNS = (
    r"\b(?:no|without|zero|free\s+of|free\s+from)\s+"
    r"(?:any\s+)?(?:limbs?|arms?|hands?|legs?|feet|wheels?|holes?|gaps?|"
    r"backgrounds?|text|logos?|watermarks?|shadows?|people|persons?|humans?)\b",
    r"\b(?:limbs?|arms?|hands?|legs?)-free\b",
    r"\b(?:removed?|remove|excluding|omit(?:ted)?)\s+"
    r"(?:the\s+)?(?:limbs?|arms?|hands?|legs?|wheels?|backgrounds?)\b",
    # 中文残留（万一没翻译干净）
    r"(?:去掉|不要|没有|无)\s*(?:四肢|手脚|手臂|腿|轮子|背景|文字)",
)


def _strip_negations(prompt: str) -> str:
    """把提示词里的否定片段删掉。

    ★ 为什么删而不是改写：
      改写（"no limbs" → "seamless closed body"）需要语义理解，
      代码层硬改容易造出不通的句子；
      而**删掉**一定安全 —— 主体描述本来就在，少一个否定词不会变差 ✓
      正向改写交给 spec 里的规则（LLM 擅长），代码只做兜底清除。
    """
    if not prompt:
        return prompt
    out = prompt

    # ⓪ 剥掉句首的祈使动词。
    #
    # ★ 实测：用户说「做一个六角法兰联轴器」→ 翻译给的是 "Create a hexagonal flange coupling..."，
    #   而这两个兜底函数（_force_single_object / _reinforce_shape）都假定输入是**名词短语**，
    #   于是拼成 "a single hexagonal create a hexagonal flange coupling..." —— 语法散架，
    #   主体名词还重复了一次。用户说「一个...」时译成 "A hexagonal..." 就不会触发。
    #   提示词本来就该是名词短语，动词是多余的，直接剥掉最省事。
    # 只剥动词，**保留冠词** —— 剥掉 "a/an" 会让下面的 _force_single_object 以为
    # 没有限定词又补一次，拼出 "a single single phone stand"。带 \b 避免 "an" 被吃成 "n"。
    out = re.sub(
        r"^\s*(?:please\s+)?(?:create|generate|make|draw|design|produce|build|render)\b\s*"
        r"(?:me\b\s*)?",
        "", out, flags=re.I)

    for pat in _NEG_PATTERNS:
        out = re.sub(pat, "", out, flags=re.I)

    # 清理删完留下的空分句 / 重复分隔符
    parts = [p.strip(" .;") for p in re.split(r",", out)]
    parts = [p for p in parts if p and not re.fullmatch(r"(?:and|or|with|the|a|an)", p, re.I)]
    out = ", ".join(parts)
    out = re.sub(r"\s{2,}", " ", out).strip(" ,.")
    out = _force_single_object(out)
    return _reinforce_shape(out)


# ══════════════════════════════════════════════════════════════════════
# 几何形状词强化（2026-10-18）
# ══════════════════════════════════════════════════════════════════════
#
# ★ 实测事故：用户要「一个**六边形**笔筒，筒身有细密竖纹」
#   → 扩写出来的提示词里明明写着 `a hexagonal pen holder with fine vertical grooves`
#   → 出图却是**圆的** ✗
#
# 原因：扩散模型对"几何形状形容词"的遵循很弱。它会把 attention 更多分配给
#   主体名词（pen holder）和材质/纹样词（grooves, matte），
#   形状词夹在中间经常被"平均掉"。
#
# 修法（有依据、成本极低）：
#   ① 把形状词**提到最前面** —— 提示词开头 token 的权重最高
#   ② 在末尾**再点一次**（"hexagonal cross-section"），首尾呼应
#   ③ 补一个"形状约束"短语，把形容词变成可观测量（横截面是什么形状）
#
# 注意：这不是万灵药。模型画不画得出来还受分辨率限制，
#   但"说了六边形却画出圆的"这种明显违背，用这招能显著改善。
#
# ★ 2026-10-18 实测结论（两次实验，第二次推翻了第一次，都记下来）：
#
#   实验一：提示词里用 hexagonal（出现两次：开头主体 + 末尾 cross-section）
#     → Qwen-Image-2.1 / Z-Image 出来的**仍然是圆的** ✗
#   实验二：改用 six-sided 这种**直白几何描述**
#     → 同一台机器、同一模型，**六棱柱画得非常准确** ✓
#     （实测提示词：a six-sided pen holder, 120mm tall, with fine vertical texture on the body）
#
#   结论修正：不是"扩散模型对多棱柱有硬边界"，而是**用词选错了**。
#     hexagonal 在训练语料里偏"装饰性的六边形纹样"（瓷砖、蜂巢、花纹），
#     不是"六棱柱的横截面"，所以模型容易画成一个圆底+六边形装饰。
#     six-sided / six-sided prism 是几何描述，模型能正确理解。
#
#   ★ 但要如实记住：six-sided **也不是每次都成**。
#     同一句 "a six-sided pen holder, 120mm tall, with fine vertical texture"，
#     实测做过标准六棱柱 ✓，也出过圆角方柱 ✗ —— 是概率性的。
#     所以别写"改了这个词就一定对"；一次不成重出一次，比继续抠提示词有效。
#
#   这段 shape reinforcement 仍然有用（首尾呼应能防形状词被淹没），
#   但**真正决定成败的是词本身**：六棱柱要用 six-sided，
#   八棱柱用 eight-sided，不要用 hexagonal / octagonal 这类"纹样化"的词。
#   下面 _SHAPE_WORDS 里的映射保留（对角柱/圆/方这类无害），
#   但如果要精确多棱柱，更可靠的是让上游扩写直接用 N-sided 表达。
# ══════════════════════════════════════════════════════════════════════

_SHAPE_WORDS = {
    # ★ 修正（2026-10-18 实测）：hexagonal → **six-sided**
    #   原来映射成 "hexagonal cross-section"，还是那个"纹样化"的词，
    #   模型照样画圆的。换成直白几何描述才管用。
    "hexagonal": "six-sided, hexagonal cross-section",
    "octagonal": "eight-sided, octagonal cross-section",
    "pentagonal": "five-sided, pentagonal cross-section",
    "triangular": "three-sided, triangular cross-section",
    "square": "square cross-section",
    "rectangular": "rectangular cross-section",
    "cylindrical": "cylindrical form",
    "spherical": "spherical form",
    "cubic": "cubic form",
    "conical": "conical form",
}


def _reinforce_shape(prompt: str) -> str:
    """把几何形状词提到开头并在末尾复述，提升模型对形状的遵循度。

    只在**确实出现形状词**时生效；普通提示词原样返回（不引入噪声）。
    """
    if not prompt:
        return prompt
    low = prompt.lower()

    hit = None
    for word, phrase in _SHAPE_WORDS.items():
        if re.search(rf"\b{word}\b", low):
            hit = (word, phrase)
            break
    if not hit:
        return prompt

    word, phrase = hit

    # ① 开头已有形状词就不动（避免 "hexagonal hexagonal ..."）
    starts_with = re.match(r"^\s*a\s+(?:single\s+)?", prompt, re.I)
    head = prompt
    if starts_with:
        rest = prompt[starts_with.end():]
        if not rest.lower().startswith(word):
            head = prompt[: starts_with.end()] + word + " " + rest
    else:
        # ★ 2026-10-18 修：原来只判 low.startswith(word)，
        #   漏了 "an octagonal tray" 这种**冠词开头**的句子 →
        #   结果拼成 "octagonal an octagonal tray" ✗ 语法都是错的。
        #   现在先剥掉冠词再判断，有冠词就插在冠词后面。
        art = re.match(r"^\s*(a|an|the)\s+", prompt, re.I)
        if art:
            rest = prompt[art.end():]
            if not rest.lower().startswith(word):
                head = prompt[: art.end()] + word + " " + rest
        elif not low.startswith(word):
            head = word + " " + prompt

    # ② 末尾复述（首尾呼应，强化形状约束）
    tail = f"{phrase}, sharp geometric edges"
    if phrase.lower() not in head.lower():
        head = head.rstrip(" ,.") + ", " + tail

    return head


# ══════════════════════════════════════════════════════════════════════
# 单物体强制（2026-10-17）
# ══════════════════════════════════════════════════════════════════════
#
# ★ 为什么必须强制"只有一个物体"
#   扩散模型画复数名词时会真的画两个/一堆 ——
#   实测「带洞洞的拖鞋」(slippers) 就画出**两只鞋**，
#   而 3D 重建把两只粘成一个畸形整体，完全没法用 ✗
#
#   3D 打印/建模本来也只需要**一个**实体零件，
#   所以"从复数收敛到一个代表物体"既符合工艺术语，也符合用户真实目的 ✓
#
# ★ 只处理**主体名词**的复数，不动样式词：
#   "slippers" → "a single slipper"
#   但 "details"/"shadows"/"colors" 这类不动（它们不是主体，改了反而不通）
_SUBJECT_PLURALS = {
    "slippers": "slipper",
    "shoes": "shoe",
    "sandals": "sandal",
    "boxes": "box",
    "bottles": "bottle",
    "cups": "cup",
    "mugs": "mug",
    "plates": "plate",
    "bowls": "bowl",
    "vases": "vase",
    "pens": "pen",
    "keychains": "keychain",
    "figures": "figure",
    "toys": "toy",
    "phones": "phone",
    "stands": "stand",
    "holders": "holder",
}

# 这些词开头说明是**描述/修饰**，不是"一个物体" → 不该加 "a single"
# （见 _force_single_object 里的实测踩坑说明）
_NON_SUBJECT_OPENERS = frozenset("""
details detail shadows shadow colors colour textures texture lighting light
highlights backgrounds background props scene composition style design
soft smooth glossy matte realistic stylized simplified minimalist
""".split())


def _force_single_object(prompt: str) -> str:
    """确保提示词只描述**一个**物体。

    ★ 两件事：
      ① 已知主体名词的复数 → 单数（slippers → a single slipper）
      ② 如果开头没有 "a single/one/a"，补一个显式限定

    ★ 为什么不改写所有复数：
      "details" / "shadows" / "colors" 是修饰成分，改单数会读不通。
      只动**已知的主体名词**表 + 句首限定，风险最低 ✓
    """
    if not prompt:
        return prompt

    low = prompt.lower()
    for plural, single in _SUBJECT_PLURALS.items():
        # \b 保证整词匹配，避免 "shoes" 命中 "shoeshine"
        low = re.sub(rf"\b{plural}\b", f"a single {single}", low, flags=re.I)

    # 已经是单数形态（含 a single / one / a / an 开头）就不重复加
    if re.match(r"^\s*(a\s+single|one|a|an)\b", low, re.I):
        return low

    # 开头补限定词 —— 但**不能盲加**。
    #
    # ★ 实测踩到（2026-10-17）：
    #   "details and shadows on a box" 被加成
    #   "a single details and shadows on a box" —— 语法都错了 ✗
    #   原因：这句话是**描述短语**，不是"一个物体"，硬加"a single"就散了。
    #
    # ★ 判据：只有当开头像**名词短语**时才加。
    #   抽象词开头的（details / shadows / colors / textures / lighting…）
    #   说明这不是"一个物体"，而是修饰描述 —— 原样返回最安全 ✓
    first = re.split(r"[\s,]", low.lstrip())[0] if low.strip() else ""
    if first in _NON_SUBJECT_OPENERS:
        return low

    # 开头补 "a single" —— 明确告诉模型只要一个
    return "a single " + low.lstrip(" ,")


def _agent_cfg_light() -> dict:
    """读 config.json 的 agent 段（不 import backend.agent，避免循环依赖）"""
    try:
        cfg = json.loads((Path(__file__).resolve().parent.parent / "config.json")
                         .read_text(encoding="utf-8"))
        return cfg.get("agent", {}) or {}
    except Exception:
        return {}


def _chat_call(prompt: str, timeout: int = 90) -> str:
    """按 agent.provider 选后端做一次文本调用：tuned → 微调服务；否则 Ollama。

    为什么不能固定走 Ollama：provider=tuned 时 Ollama 里没有常驻模型，一调就会把
    **gpt-oss(12.9GB) 拉进显存并 keep_alive=-1 常驻**；而此时微调模型(11GB)还占着，
    合计 24GB > 20GB，紧跟其后的 ComfyUI 生成只剩几 GB 可用 —— 实测直接超时
    （高清重绘 600s、3D 建模 1200s，而正常是 86s 出模型）。
    """
    cfg = _agent_cfg_light()
    if cfg.get("provider") == "tuned":
        import urllib.request
        url = cfg.get("tuned_url", "http://127.0.0.1:8082")
        body = json.dumps({"messages": [{"role": "user", "content": prompt}]}).encode()
        req = urllib.request.Request(f"{url}/api/chat", data=body,
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read().decode("utf-8", "ignore"))
        return (d.get("message") or {}).get("content", "") or ""
    return _call_ollama(prompt, timeout=timeout)


def enhance_image_prompt(zh_prompt: str, timeout: int = 90) -> str:
    """中文需求 → 详细英文生图提示词。失败返回空串（调用方用 fallback 兜底，绝不阻塞生成）

    2026-09-25 修：
      · 失败时不再返回空串 —— 调用方之前 fallback 到裸中文短句给 Qwen-Image-2.1，
        因为模型自己"补全"的方向容易偏（茶杯垫→陶土饼、熊大→美式萌熊）。
      · 这里加一个轻量 IP 别名识别 + 安全英文模板，让 fallback 至少稳定。
    """
    zh = (zh_prompt or "").strip()
    if not zh:
        # ★★ 2026-10-15 加：空输入必须吼一声，不能静默返回 "" ★★
        #
        # 为什么：原来这里直接 `return ""`，**不抛异常也不打日志**。
        # 调用方那句 `if en_prompt:` 不成立、`except` 分支也不会走，
        # 于是日志里干干净净 —— 出图跑题时完全没有线索。
        # 实测就是它导致的："要浣熊出卡通小男孩、要大象出女战士"，
        # 而日志里既没有 ✨ 也没有 ⚠️，查了很久才定位到"提示词里没有主体"。
        print("[DSH] ⚠️ 生图提示词为空（本组没有需求）→ 返回空串，调用方会拒绝生成")
        return ""
    if zh in _IMG_EN_CACHE:
        return _IMG_EN_CACHE[zh]
    # 快速判断：是不是已知 IP？优先用英文别名（spec 里特别要求）
    ip_english = _detect_known_ip(zh)
    if ip_english:
        # IP 角色：直接给一个稳的英文 prompt，不走对话模型（避免超时 80% 概率）
        base = ip_english["base_en"]
        extra = _ip_specific_pose(zh, ip_english)
        # ★ 2026-10-15：把用户说的属性也拼进去。
        #   原来是 base + 姿态 + 固定尾巴 —— 用户写的"红色马甲"这类**几何差异**
        #   完全没进提示词，出图和需求对不上（用户报"准确性不行"的根因之一）。
        attrs = _ip_specific_attrs(zh)
        parts = [base]
        if attrs:
            parts.append(attrs)
        parts.append(extra)
        parts.append("clean light background, ultra detailed, sharp focus")
        en = ", ".join(parts)
        _IMG_EN_CACHE[zh] = en
        print(f"[DSH] 命中已知 IP 角色「{ip_english['zh_name']}」→ 模板直出"
              f"（含用户属性: {attrs or '无'}）")
        return en
    try:
        raw = _chat_call(f"{_IMG_PROMPT_SPEC}\n\nUser requirement (Chinese):\n{zh}\n\nEnglish prompt:", timeout=timeout)
        en = _clean_image_prompt(raw)
        if en:
            _IMG_EN_CACHE[zh] = en
            return en
        print("[DSH] ⚠️ 生图提示词扩写/英译结果不可用，回退安全模板")
    except Exception as e:
        print(f"[DSH] ⚠️ 生图提示词扩写异常: {e!r}，回退安全模板")
    # 兜底：不再返回空串；走通用安全模板（保底英文描述）
    en = _safe_english_template(zh)
    if en:
        _IMG_EN_CACHE[zh] = en
    return en


# ══════════════════════════════════════════════════════════════════════
# 未知角色的「如实说 + 要细节」（宇凯 2026-10-16 要求）
# ══════════════════════════════════════════════════════════════════════
#
# 宇凯的原话：
#   "重点不是补上IP而是确认需求，如果是已知的IP还好说，但是如果是未知的
#    我们要实事求是的说不知道，然后根据用户的反馈以及细节描述来生成"
#
# ── 为什么补 IP 表不够 ──
#   IP 表是**有限**的，而用户想做什么是无法穷举的。
#   补表只能覆盖已想到的角色，遇到表外的照样出问题 ✗
#   真正的解法是**承认不知道**，并用用户自己的描述去生成。
#
# ── 实测：表外角色有两条坏路径 ──
#   ① 被**认成别的角色**（钢铁侠→Minion / yellow）—— 有错误词，还算好发现
#   ② 主体**被整个抹掉**：实测「原神里的钟离，岩王帝君造型」扩写出来是
#        "stylized cartoon character, ..." —— 没有任何主体，模型随机发挥 ✗
#      这条最隐蔽：输出看着"正常"，但主体没了，用户拿到个完全无关的东西。
#
# 所以要能**检测主体是否还在**，丢了就说不知道、要细节。

# 通用空壳词：只有这些词说明主体没被表达出来。
# 它们在"主体丢失"的输出里必现，而在正常输出里会伴随具体主体名。
_SUBJECTLESS_ONLY = (
    "stylized cartoon character", "cartoon character", "a character",
    "humanoid", "creature", "figure of a character", "mascot",
)

# 每张提示词都会出现的"填充词"（风格/画质/材质/背景/姿态）。
#
# ★ 为什么要单独列出来：判"主体在不在"时不能把这些算作证据 ——
#   否则 "stylized cartoon character, ... smooth forms, collectible figurine"
#   里 smooth/forms/collectible 一堆词，会被误判成"主体在" ✗
#   只有**这些之外**的词（creeper / bluey / hexagon / iron…）才是主体线索。
_SHELL_WORDS = frozenset("""
stylized cartoon character faithful original design smooth simplified forms
collectible toy figurine realistic human anatomy skin pores standing portrait
plain white light clean background ultra detailed sharp focus high quality
render render3d model figurine figure pose mild scene props environmental
details subtle product shot industrial material matte glossy plastic resin
abs pla metal metallic armour armor armor panels hard surface look physical
well made beautiful vibrant color colour size medium small large tall
no without never flat front side view angle camera lighting soft shadow
seamless minimal minimalist design styled style looks looking appear
character characters object item thing piece standing sitting crouch
""".split())


def subject_lost(zh: str, en: str) -> bool:
    """判断扩写后的英文提示词里**主体是否没了**。

    ★ 判据不用 LLM（那又要一次调用、又要超时），用**字符从用户描述里
      迁移过去没有**这个可计算的信号：

        用户写「原神里的钟离，岩王帝君造型」
          → 具体词「钟离」「岩王帝君」应当以某种形式出现在英文里
          → 若英文里**一个都没体现**（只剩 stylized cartoon character），
            就是主体丢了 ✓

    ★ 为什么不能只查空壳词：
      "iron man, stylized cartoon character, ..." 里有空壳词但主体在，
      不算丢。所以真正的判据是"**具体词有没有落地**" ✗
    """
    if not en.strip():
        return True

    # ① 用户描述里的"具体名词"——去掉修饰词/量词/风格词，留实词
    body = re.sub(r"[\s，,。.、—\-]+", "", zh)
    # 去掉常见非主体词（尺寸/风格/颜色/要求），剩下的才可能是主体
    body = re.sub(
        r"(写实|卡通|机械|风格|配色|颜色|尺寸|零件|摆件|手办|模型|造型|"
        r"全身|站立|坐姿|简约|偏|的|一个|一只|我要|请|生成|图片|预览|"
        r"高(\d+)|毫米|mm|\d+)",
        "", body,
    )
    tokens = [t for t in re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z]{3,}", body) if len(t) >= 2]

    en_low = en.lower()
    # ② 有任一具体词以拼音/英文/原词形式出现 → 主体保住了
    for t in tokens:
        if t.lower() in en_low:
            return False

    # ②b ★ 被**翻译**了也算保住（2026-10-16 实测踩到）
    #
    #   「我的世界里的苦力怕」→ "minecraft creeper" —— 完全正确，
    #   但中文"苦力怕"不可能出现在英文里，于是被 ① 判成"主体丢了" ✗
    #   这就是**误报**：会把正常生成拦下来，比漏报更烦人。
    #
    #   判据：英文里出现了**具体名词**（非空壳词）就说明主体在。
    #   空壳词之外的实词（creeper / bluey / hexagon…）都算主体落地 ✓
    words = [w for w in re.findall(r"[a-z]{3,}", en_low)]
    concrete = [w for w in words if w not in _SHELL_WORDS]
    # 需要**至少两个**不同的实词，避免单个形容词误判
    if len(set(concrete)) >= 2:
        return False

    # ③ 英文里都是空壳词 → 丢了
    if any(s in en_low for s in _SUBJECTLESS_ONLY):
        return True

    # ④ 判不了 → 保守认为没丢（宁可放过，不要误拦正常生成）
    return False


def describe_unknown_subject(zh: str) -> str:
    """认不出主体时给用户的**实话 + 引导**（不改代码流程，只出文案）。

    ★ 为什么是"要细节"而不是"我尽力"：
      我们已经实测过"尽力猜"的后果 —— 猜错出小黄人、或者主体被抹掉
      随机生成。用户花几分钟等出图建模，拿到完全无关的东西，
      比一开始就告诉他"我需要你多说几句"要糟得多 ✗
    """
    return (
        "这个角色我**没有把握**准确认出来 —— 我不敢硬着头皮画，"
        "因为那样出来的多半跟你想要的不是一回事。\n\n"
        "你可以用**外观描述**告诉我，我按你说的做，比如：\n"
        "· 体型（人形／动物／方块状）\n"
        "· 主色调（红金相间／荧光绿／白蓝）\n"
        "· 标志性特征（胸口发光圆形／头顶有角／背后有披风）\n"
        "· 材质感（金属装甲／毛绒／塑料）\n\n"
        f"你现在说的是：「{zh[:60]}」—— 在这个基础上补几句就行。"
    )

# —— 下面是 2026-09-25 新增的小工具 ——

# 已知 IP 角色的英文别名（导入主程序里那张表 —— 那张表是 Web 搜索用的，
# 这里只取用得上的"中文→英文"映射）。不在表里的角色照样走对话模型扩写。
try:
    from .web_ref import CHARACTER_NAME_ALIASES as _IP_TABLE
except Exception:
    _IP_TABLE = {}


def _detect_known_ip(zh: str) -> dict | None:
    """命中已知 IP 角色 → 返回 base_en（英文官方名）+ 该 IP 自己的风格。

    ★ 2026-10-16 修：以前**所有** IP 都硬拼 "Boonie Bears cartoon style" ——
      那张表当初只有熊出没，所以写死了。现在表里加了漫威/日漫，
      再拼熊出没风格会把钢铁侠画成熊 ✗ 改成按 IP 归类给风格。
    """
    zh_clean = (zh or "").strip()

    def _pack(english: str, zh_name: str) -> dict:
        return {
            "base_en": f"{english}, {_style_for_ip(english)}",
            "zh_name": zh_name,
            "english": english,
        }

    # 直接命中表（"熊大" → ["Briar Bear", "熊大 熊出没 角色"]）
    if zh_clean in _IP_TABLE:
        aliases = _IP_TABLE[zh_clean]
        english = aliases[0] if aliases else zh_clean      # 第一项是英文别名
        return _pack(english, zh_clean)
    # 用户描述里**提到** IP 角色（如"我要熊大摆件"、"钢铁侠——穿金色机甲"）
    #
    # ★ 按名字长度降序匹配（2026-10-16 加）：
    #   "托尼·斯塔克" 和 "钢铁侠" 都在表里时，长名先匹配才不会
    #   被短名截胡；稳定且可预测 ✓
    for name in sorted(_IP_TABLE.keys(), key=len, reverse=True):
        if name in zh_clean:
            aliases = _IP_TABLE[name]
            english = aliases[0] if aliases else name
            return _pack(english, name)
    return None


# IP → 画面风格后缀。
#
# ★ 为什么要按 IP 分类而不是一句话通用：
#   SDXL/FLUX 对风格词很敏感 —— 给钢铁侠拼 "cartoon style" 会削弱金属机甲质感，
#   给熊大拼 "metallic armor" 又会变成铁熊 ✗ 各归各类最稳。
_MARVELISH = ("Iron Man", "Spider-Man", "Captain America", "Thor", "Hulk",
              "Black Widow", "Batman", "Superman", "Ultraman")
_ANIMEISH = ("Doraemon", "Pikachu", "Mario", "Conan")


def _style_for_ip(english: str) -> str:
    if any(k.lower() in english.lower() for k in _MARVELISH):
        # 机甲/超级英雄：强调金属、装甲、反应堆，别用卡通词
        return ("Marvel superhero style, glossy metallic armor, "
                "smooth hard-surface panels, collectible action figure look")
    if any(k.lower() in english.lower() for k in _ANIMEISH):
        return "anime character style, faithful to the original design"
    # 熊出没 / 海绵宝宝 / 小猪佩奇 这类 3D 卡通
    return "3D cartoon style, faithful to the original character design"


# ── 2026-10-15 新增：IP 角色的「细节找回」 ──────────────────────────────
#
# 问题：命中已知 IP 后走的是**固定模板**（base_en + 一个姿态词），
#       用户在这个角色上说的属性**一个都进不了提示词**。
#       实测（熊大）：用户写「棕色、圆脸、**红色马甲**、开朗活泼」，实际提交的是
#         "Briar Bear, Boonie Bears cartoon style, standing in a friendly pose, ..."
#       —— 马甲彻底丢了，出的图和用户要的不是一回事，用户描述成"准确性不行"。
#
# 为什么不改成走对话模型：IP 分支存在的理由就是**绕开对话模型的超时**
#       （历史上 80% 概率超时后回退模板）。所以这里用**确定性查表**：
#       零延迟、零不确定、不引入新的超时风险。
#
# 排序依据（重要）：这条链是 图 → 3D。颜色在 3D 阶段会丢，
#       而"有没有马甲"是**几何差异**，直接决定建模结果。
#       所以衣物/配件放最前，色彩只是给用户看的时候认得出来。
_ATTR_LEXICON = [
    # 衣物 / 配件（几何相关，优先级最高）
    ("马甲", "wearing a vest"), ("背心", "wearing a vest"),
    ("外套", "wearing a coat"), ("上衣", "wearing a top"),
    ("衬衫", "wearing a shirt"), ("裤子", "wearing pants"),
    ("裙子", "wearing a skirt"), ("帽子", "wearing a hat"),
    ("围巾", "wearing a scarf"), ("手套", "wearing gloves"),
    ("领带", "wearing a tie"), ("腰带", "wearing a belt"),
    ("背包", "carrying a backpack"), ("书包", "carrying a backpack"),
    ("眼镜", "wearing glasses"), ("鞋子", "wearing shoes"),
    ("翅膀", "with wings"), ("尾巴", "with a tail"),
    # 配色（3D 阶段会丢，但出图阶段决定像不像）
    ("棕色", "brown"), ("褐色", "brown"), ("红色", "red"), ("橙色", "orange"),
    ("黄色", "yellow"), ("绿色", "green"), ("蓝色", "blue"), ("紫色", "purple"),
    ("粉色", "pink"), ("黑色", "black"), ("白色", "white"), ("灰色", "grey"),
    ("金色", "golden"), ("银色", "silver"),
    # 形体
    ("圆脸", "round face"), ("长脸", "long face"), ("大耳朵", "big ears"),
    ("胖", "chubby body"), ("瘦", "slim body"),
    # 材质（打印出来的观感）
    ("木质", "wooden"), ("金属", "metallic"), ("陶瓷", "ceramic"),
    ("玻璃", "glass"), ("皮革", "leather"),
]


def _ip_specific_attrs(zh: str) -> str:
    """从用户原话里抽**角色属性**（衣物/配件/配色/形体/材质）→ 英文短语。

    ★ 2026-10-15 新增。命中 IP 时原来只用固定模板 + 姿态词，
      用户写的属性（尤其"红色马甲"这种**几何差异**）全丢，
      出图和需求对不上。这里做确定性查表，不打模型。
    """
    hits = [en for zh_kw, en in _ATTR_LEXICON if zh_kw in zh]
    # 去重但保序：同义词（马甲/背心）命中两次只留一条
    return ", ".join(dict.fromkeys(hits))


def _ip_specific_pose(zh: str, ip: dict) -> str:
    """从用户 prompt 里抽"动作词"（挥手/跳跃/...），拼到 IP prompt 上。"""
    pose_keywords = [
        ("挥手", "waving his hand, happy smile"),
        ("跳跃", "jumping with excitement"),
        ("站立", "standing in a friendly pose"),
        ("坐", "sitting comfortably"),
        ("抱", "holding something, cute pose"),
        ("笑", "happy smiling face"),
        ("跑", "running with energy"),
        ("睡觉", "peacefully sleeping"),
    ]
    extras = [en for kw, en in pose_keywords if kw in zh]
    if not extras:
        extras = ["standing in a friendly pose"]
    return ", ".join(extras)


# 通用安全模板（enhance 超时/失败时用，绝不返回空串）
_SAFE_TEMPLATE = (
    "{subj_en}, clean light background, soft studio lighting, "
    "ultra detailed, sharp focus, product shot"
)
# 物件 → 通用材质（不是产品摄影的塑料感！）
_DEFAULT_MATERIALS = {
    "茶杯垫": "cork",
    "鼠标垫": "fabric",
    "马克杯": "ceramic, glossy",
    "手机壳": "matte plastic",
    "钥匙扣": "matte plastic",
    "小摆件": "matte resin",
    "笔筒": "wooden",
    "相框": "wooden frame",
    "花盆": "terracotta ceramic",
    "镇纸": "marble",
    "胸针": "enamel",
    "挂件": "soft silicone",
}


# 中文物件名 → 英文通用名（用户说"茶杯垫"→模型不知道是 cork 杯垫，
# 这里给扩散模型一个熟悉的词）
_OBJECT_EN = {
    # ── 动物（2026-10-15 补：原来一条都没有）──
    #
    # ★★ 这就是"要浣熊出小男孩、要大象出女战士"的直接原因 ★★
    #   原表 38 条**全是物件**（杯垫/笔筒/齿轮…），一个动物都没有。
    #   而下面 _safe_english_template 里"词表没命中"的分支，以前是**把中文原样留着**的
    #   （那个分支的注释自己都承认"中文实体名扩散模型可能不认"）。
    #   于是 enhance 那一步只要失败一次（GPU 被占时很常见），
    #   "一只大象，写实风格…" 就带着中文进了模型 → 模型自己乱补一个主体。
    #
    # ★ 匹配规则是 `k in zh` + **最长优先**，所以单字词是安全的：
    #   "猫头鹰" 命中更长的 "猫头鹰"，不会被 "猫" 抢走。
    "大象": "elephant", "小象": "baby elephant", "浣熊": "raccoon",
    "小猫": "kitten", "猫咪": "cat", "猫": "cat",
    "小狗": "puppy", "狗狗": "dog", "狗": "dog",
    "大熊猫": "giant panda", "熊猫": "giant panda",
    "北极熊": "polar bear", "小熊": "bear cub", "熊": "bear",
    "小白兔": "white rabbit", "兔子": "rabbit", "兔": "rabbit",
    "小狐狸": "fox", "狐狸": "fox",
    "老虎": "tiger", "狮子": "lion", "豹": "leopard", "狼": "wolf",
    "猴子": "monkey", "猩猩": "gorilla", "长颈鹿": "giraffe", "斑马": "zebra",
    "马": "horse", "驴": "donkey", "牛": "cow", "绵羊": "sheep", "山羊": "goat",
    "羊": "sheep", "小猪": "piglet", "猪": "pig",
    "松鼠": "squirrel", "刺猬": "hedgehog", "考拉": "koala", "袋鼠": "kangaroo",
    "骆驼": "camel", "河马": "hippo", "犀牛": "rhino", "树懒": "sloth",
    "猫头鹰": "owl", "老鹰": "eagle", "鹦鹉": "parrot",
    "小鸟": "bird", "鸟": "bird", "天鹅": "swan", "鸭子": "duck", "鸭": "duck",
    "公鸡": "rooster", "鸡": "chicken", "鹅": "goose",
    "海豚": "dolphin", "鲸鱼": "whale", "鲨鱼": "shark", "海豹": "seal",
    "章鱼": "octopus", "螃蟹": "crab", "虾": "shrimp", "水母": "jellyfish",
    "乌龟": "turtle", "龟": "turtle", "青蛙": "frog", "蛇": "snake",
    "金鱼": "goldfish", "鱼": "fish",
    "蝴蝶": "butterfly", "蜜蜂": "bee", "蚂蚁": "ant", "蜘蛛": "spider",
    "蜗牛": "snail", "霸王龙": "T-rex", "恐龙": "dinosaur",
    "独角兽": "unicorn", "龙": "dragon",
    # ── 人物 / 角色 ──
    "女孩": "girl", "男孩": "boy", "女人": "woman", "男人": "man",
    "公主": "princess", "骑士": "knight", "战士": "warrior", "忍者": "ninja",
    "机器人": "robot", "机甲人": "mecha robot", "骷髅": "skull figure",
    # 家居
    "茶杯垫": "cup coaster",
    "鼠标垫": "mouse pad",
    "马克杯": "coffee mug",
    "咖啡杯": "coffee mug",
    "水杯": "water cup",
    "杯垫": "cup coaster",
    "茶几": "coffee table",
    "凳子": "stool",
    "椅子": "chair",
    "灯": "lamp",
    "台灯": "desk lamp",
    "瓶子": "bottle",
    "相框": "photo frame",
    # 个人
    "手机壳": "phone case",
    "钥匙扣": "keychain",
    "胸针": "brooch",
    "挂件": "pendant",
    "项链": "necklace",
    # 桌面
    "笔筒": "pen holder",
    "镇纸": "paperweight",
    "笔架": "pen holder",
    "鼠标": "computer mouse",
    "键盘": "mechanical keyboard",
    # 园艺/容器
    "花盆": "flower pot",
    "花瓶": "vase",
    # 通用
    "小摆件": "small figurine",
    "摆件": "figurine",
    "手办": "figurine",
    "模型": "figurine",
    # 工业/机械（机器翻译也常常不靠谱的）
    "高达": "Gundam mecha",  # 优先：固定搭配"Gundam"
    "机械手臂": "robotic arm",
    "机械臂": "robotic arm",
    "底座": "base mount",
    "支架": "bracket mount",
    "齿轮": "gear wheel",
    "外壳": "machine housing",
    "机甲": "mecha robot",
    # 通用（放最后，匹配兜底）
    "机械": "mechanical part",
}


def _has_cn(s: str) -> bool:
    """字符串里还有没有中文。判断"能不能直接喂给扩散模型"用。"""
    return bool(re.search(r"[\u4e00-\u9fff]", s or ""))


def _safe_english_template(zh: str) -> str:
    """兜底英文模板（enhance 超时/失败时用）。
    关键：所有中文字必须替换成英文 —— Qwen-Image-2.1 也支持中文，但短中文易让模型自己"补全"偏移（陶土饼茶杯垫）。
    """
    zh = (zh or "").strip()
    if not zh:
        return ""
    # 优先匹配英文 IP 别名
    ip = _detect_known_ip(zh)
    if ip:
        extra = _ip_specific_pose(zh, ip)
        return f"{ip['base_en']}, {extra}, clean light background, ultra detailed, sharp focus"
    # 普通物件：英文 + 材质
    mat = ""
    for k, v in _DEFAULT_MATERIALS.items():
        if k in zh:
            mat = v; break
    # 用最长匹配；同长度时按字典倒序（"高达" 字典序大于 "模型" → "高达" 胜出）
    subj_en = None
    candidates = sorted([k for k in _OBJECT_EN if k in zh], key=lambda x: (len(x), x), reverse=True)
    if candidates:
        subj_en = _OBJECT_EN[candidates[0]]
    if subj_en is None:
        # 兜底：去掉中文里所有"我/帮/一个/做..."修饰
        subj_en = zh
        for noise in ("我要", "帮我", "想要", "一个", "做个", "卡通"):
            subj_en = subj_en.replace(noise, "")
        subj_en = subj_en.strip()

        """
        ★★ 2026-10-15 修：绝不把中文原样喂给模型 ★★

          这里以前是 `subj_en = zh`（把中文留着），注释写着
          "中文实体名扩散模型可能不认（"Xiongda"那种失败），但至少材质/风格没错"。
          实测这个赌注是错的、而且**后果比"认不出"严重得多**：
          带着中文的短提示词会让模型**自己补一个完全无关的主体** ——
          要浣熊出卡通小男孩、要大象出红发女战士，最后建模建的就是那个错的东西。
          "主体跑题"比"主体认不出"坏得多：用户要的是这个东西。

          现在的做法：
            ① 先试翻译（词表没命中才走到这儿，属于罕见路径，多花几秒可以接受）
            ② 翻译不出来就**把中文删掉**，只留风格/材质描述 ——
               宁可让模型按风格自由发挥，也不要它按一个它读不懂的字符串乱补。
        """
        if _has_cn(subj_en):
            try:
                from backend.prompt_enhancer import translate as _tr
                _en = _tr(subj_en, "en")
                if _en and not _has_cn(_en):
                    subj_en = _en.strip()
            except Exception as e:
                # ★ 不要静默吞异常。
                #   写这段时踩过：同一处漏改一个函数名（self_has_cn）→ NameError
                #   → 翻译结果**无声丢弃**，表现和"翻译失败"完全一样，极难发现。
                #   和本文档反复强调的"静默失败最难查"是同一个坑。
                print(f"[DSH] ⚠️ 兜底翻译失败（忽略，转去删中文）: {e!r}")
        if _has_cn(subj_en):
            # 翻不出来 → 删掉中文，别让模型读到它
            stripped = re.sub(r"[\u4e00-\u9fff，。、！？；：（）()【】]+", " ", subj_en)
            subj_en = " ".join(stripped.split()).strip()
        subj_en = subj_en or "object"
    mat_phrase = f" made of {mat}" if mat else ""
    return _SAFE_TEMPLATE.format(subj_en=subj_en + mat_phrase)


# ── Strata（Qwen3.8-Flash-Next 125B）OpenAI 兼容后端 ─────────────────
# 2026-10-23：把 agent 的 27B Ollama 换成 Strata IQ3_S（125B MoE，V100 32G）。
# 它支持 response_format=json_object，且 thinking 默认关（reasoning_effort=none）。

STRATA_DEFAULT_URL = "http://127.0.0.1:8091/v1"
STRATA_DEFAULT_MODEL = "qwen3.8-flash-next-iq3_s"


def _strata_cfg() -> tuple[str, str]:
    cfg = _agent_cfg_light()
    base = str(cfg.get("strata_url") or STRATA_DEFAULT_URL).rstrip("/")
    model = str(cfg.get("strata_model") or STRATA_DEFAULT_MODEL)
    return base, model


def _strata_request(prompt: str, timeout: int, stream: bool,
                    json_mode: bool, max_tokens: int = 1800):
    import urllib.request
    base, model = _strata_cfg()
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": stream,
        "temperature": 0.2,
        "max_tokens": int(max_tokens),
        "reasoning_effort": "none",
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    req = urllib.request.Request(
        f"{base}/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    return urllib.request.urlopen(req, timeout=timeout)


def _call_strata(prompt: str, timeout: int = 180, json_mode: bool = False) -> str:
    global LAST_THINKING
    try:
        with _strata_request(prompt, timeout, False, json_mode) as resp:
            d = json.loads(resp.read().decode("utf-8", "ignore"))
        msg = ((d.get("choices") or [{}])[0].get("message") or {})
        LAST_THINKING = (msg.get("reasoning_content") or "").strip()
        return (msg.get("content") or "").strip()
    except Exception as e:
        print(f"[DSH] Strata 调用失败: {e!r}")
        return f"[描述生成失败: {e}]"


def _stream_strata(prompt: str, timeout: int, status: dict, json_mode: bool = False):
    import urllib.request
    acc = ""
    try:
        with _strata_request(prompt, timeout, True, json_mode) as resp:
            for raw in resp:
                line = raw.decode("utf-8", "ignore").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if not data or data == "[DONE]":
                    if data == "[DONE]":
                        break
                    continue
                try:
                    obj = json.loads(data)
                except Exception:
                    continue
                delta = ((obj.get("choices") or [{}])[0].get("delta") or {})
                tp = delta.get("reasoning_content") or ""
                if tp:
                    status["thinking"] = status.get("thinking", 0) + len(tp)
                    status["_thinking_text"] = (status.get("_thinking_text", "") + tp)[-1200:]
                piece = delta.get("content") or ""
                if piece:
                    acc += piece
                    status["chars"] = len(acc)
                    yield acc
    except Exception as e:
        print(f"[DSH] Strata 流式失败: {e!r}")
        yield f"[描述生成失败: {e}]"


def _strata_alive() -> bool:
    try:
        import urllib.request
        base, _model = _strata_cfg()
        with urllib.request.urlopen(f"{base}/models", timeout=5) as r:
            d = json.loads(r.read().decode("utf-8", "ignore"))
        return any(((m.get("status") or {}).get("value") == "loaded")
                   for m in (d.get("data") or []))
    except Exception:
        return False


# ── 一块 V100 只能住一个大脑：Ollama 19GB / Strata 29GB / ComfyUI 生成，三者轮流 ──
STRATA_ROOT = Path(r"D:\Strata-data\terminal\0.1.47")


def ollama_stop(model: str = None) -> bool:
    """让 Ollama 立刻卸载模型（keep_alive=0），给 Strata 腾出约 19GB。"""
    try:
        import urllib.request
        m = model or OLLAMA_MODEL
        req = urllib.request.Request(
            f"{OLLAMA_URL}/api/generate",
            data=json.dumps({"model": m, "prompt": "", "stream": False,
                             "keep_alive": 0, "options": {"num_predict": 1}}).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=15).read()
        print(f"[DSH] 已让 Ollama 卸载 {m}（给 Strata 腾显存）")
        return True
    except Exception as e:
        print(f"[DSH] 卸载 Ollama 失败（可能本来就没加载）: {e!r}")
        return False


def strata_stop(timeout: float = 30.0) -> bool:
    """停掉 Strata 服务，释放约 29GB 显存（生成图片/3D 前调用）。"""
    import time
    try:
        import psutil
    except Exception:
        return False
    def listeners():
        out = []
        for c in psutil.net_connections(kind="inet"):
            if c.laddr and c.laddr.port == 8091 and c.status == "LISTEN" and c.pid:
                out.append(c.pid)
        return set(out)
    pids = listeners()
    if not pids:
        return True
    for pid in pids:
        try:
            p = psutil.Process(pid)
            for ch in p.children(recursive=True):
                try: ch.terminate()
                except Exception: pass
            p.terminate()
        except Exception:
            pass
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not listeners():
            print("[DSH] Strata 已停，显存让给 ComfyUI")
            return True
        time.sleep(0.5)
    for pid in listeners():
        try:
            p = psutil.Process(pid)
            for ch in p.children(recursive=True):
                try: ch.kill()
                except Exception: pass
            p.kill()
        except Exception:
            pass
    time.sleep(1)
    return not listeners()


def _gpu_used_mib() -> int:
    """当前整卡已用显存（启动 Strata 前用来判断有没有别的模型在跑）。"""
    try:
        import subprocess
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            timeout=10, text=True)
        return int(out.strip().splitlines()[0])
    except Exception:
        return 0


def strata_start(timeout: int = 240, shared: bool | None = False) -> bool:
    """把 Strata 服务拉起来并等到模型 ready（约 60-120s）。

    shared=False（默认）：24GB 配置（留 ~8GB 给上下文，~20–29 tok/s）；
    shared=True：19GB 配置，给 ComfyUI/其他模型留 ~12GB；
    None：自动——启动前整卡已用 >6GB 就选 19GB 配置，否则 24GB。
    """
    import subprocess, time
    if _strata_alive():
        return True
    py = STRATA_ROOT / "python" / "python.exe"
    srv = STRATA_ROOT / "serve" / "server.py"
    full_cfg = STRATA_ROOT / "strata-iq3_s.json"
    mid_cfg = STRATA_ROOT / "strata-iq3_s_24g.json"
    shared_cfg = STRATA_ROOT / "strata-iq3_s_shared.json"
    if shared is None:
        shared = _gpu_used_mib() > 6000 and shared_cfg.exists()
    cfg = shared_cfg if (shared and shared_cfg.exists()) else (
        mid_cfg if mid_cfg.exists() else full_cfg)
    if not (py.exists() and srv.exists() and cfg.exists()):
        print("[DSH] Strata 安装不完整，无法启动")
        return False
    print(f"[DSH] 启动 Strata（{'19GB 共享' if shared else '满缓存'} 配置）")
    log = open(STRATA_ROOT / "strata-serve.log", "ab")
    flags = 0x00000008 | 0x00000200
    try:
        subprocess.Popen([str(py), str(srv), "--engine", "strata", "--config", str(cfg),
                          "--port", "8091"], cwd=str(STRATA_ROOT), stdout=log,
                         stderr=subprocess.STDOUT, creationflags=flags)
    except Exception as e:
        print(f"[DSH] 启动 Strata 失败: {e!r}")
        return False
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _strata_alive():
            print("[DSH] Strata 125B 已就绪")
            return True
        time.sleep(2)
    print("[DSH] Strata 启动超时")
    return False


def strata_switch(shared: bool, timeout: int = 240) -> bool:
    """切换满缓存 / 19GB 配置。没有运行时热缩，只能停→起（约 80s）。"""
    if _strata_alive():
        print("[DSH] 切换 Strata 配置（约 80s）…")
        strata_stop()
    return strata_start(timeout=timeout, shared=shared)


def unload_ollama() -> bool:
    """卸载 Ollama 模型，释放内存/显存（关铸梦时调用）"""
    if _agent_cfg_light().get("provider") == "strata":
        print("[DSH] provider=strata：模型常驻 Strata，不在 Ollama，跳过卸载")
        return False
    try:
        import urllib.request
        req = urllib.request.Request(
            f"{OLLAMA_URL}/api/generate",
            data=json.dumps({
                "model": OLLAMA_MODEL,
                "prompt": "",
                "stream": False,
                "think": False,
                "keep_alive": 0,
                "options": {"num_predict": 1}
            }).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        urllib.request.urlopen(req, timeout=10)
        return True
    except Exception:
        return False


def _training_lock_active() -> bool:
    """有微调训练在跑？训练独占 GPU（14B ≈ 15GB），任何对话模型加载都会把训练拖慢 3 倍以上
    （实测步时 14s→53s、显存 98%）。锁由 finetune/train_v8.py 维护。
    """
    return (Path(__file__).resolve().parent.parent / "finetune" / "TRAINING.lock").exists()


def warm_up_ollama(timeout: int = 240) -> bool:
    """预热：把模型加载进内存并常驻（keep_alive=-1）。

    不做预热的话，用户第一次输入要等 ~27s 冷启动；预热后单次响应 ~1-2s。
    档位按 config 取（推理模型用其配置的 think，避免个别模型不接受 think=false）。
    """
    if _training_lock_active():
        print("[DSH] 检测到微调训练进行中，跳过对话模型预热（把显存留给训练）")
        return False
    if _agent_cfg_light().get("provider") == "strata":
        return _strata_alive()
    try:
        import urllib.request
        p = _profile()
        req = urllib.request.Request(
            f"{OLLAMA_URL}/api/generate",
            data=json.dumps({
                "model": OLLAMA_MODEL,
                "prompt": "hi",
                "stream": False,
                "think": p["think"],
                "keep_alive": -1,
                "options": {"num_predict": 1}
            }).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        urllib.request.urlopen(req, timeout=timeout)
        return True
    except Exception:
        return False


def is_model_resident() -> bool:
    """检查模型是否已驻留内存（未驻留 → 下次调用会冷启动）"""
    if _agent_cfg_light().get("provider") == "strata":
        return _strata_alive()
    try:
        import urllib.request
        with urllib.request.urlopen(f"{OLLAMA_URL}/api/ps", timeout=5) as r:
            data = json.loads(r.read())
        return any(OLLAMA_MODEL.split(":")[0] in m.get("name", "") for m in data.get("models", []))
    except Exception:
        return False


def _stream_once(prompt: str, model: str, think, num_predict: int,
                 temperature: float, timeout: int, status: dict, num_ctx: int = None,
                 json_mode: bool = False):
    """单次流式调用，逐块 yield「累积文本」。出没出正文记在 status['chars']。"""
    if _training_lock_active():
        # 训练期间不流式加载模型（会把 12.9GB 拉回显存）。产出非 JSON 文本，
        # 上层解析失败后回退关键词路径。
        print("[DSH] 微调训练进行中，跳过流式对话模型调用")
        yield "[训练进行中，对话模型暂时不可用]"
        return
    if _agent_cfg_light().get("provider") == "strata":
        yield from _stream_strata(prompt, timeout, status, json_mode=json_mode)
        return
    import urllib.request
    opts = {"temperature": temperature, "num_predict": num_predict}
    if num_ctx:                      # 与非流式一致：显式放宽上下文，否则长 prompt 会被截断
        opts["num_ctx"] = int(num_ctx)
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/generate",
        data=json.dumps({
            "model": model, "prompt": prompt, "stream": True,
            "think": think, "keep_alive": -1,
            "options": opts,
            **({"format": "json"} if json_mode else {}),
        }).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST"
    )
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
            if obj.get("thinking"):
                tp = obj["thinking"]
                status["thinking"] = status.get("thinking", 0) + len(tp)
                # 留尾部（收尾才是判断依据），供上层做"🧠 思考"展示
                status["_thinking_text"] = (status.get("_thinking_text", "") + tp)[-1200:]
            piece = obj.get("response") or ""
            if piece:
                acc += piece
                status["chars"] = len(acc)
                yield acc
            if obj.get("done"):
                break


def _stream_ollama(prompt: str, timeout: int = 180):
    """流式调用（首字 <1s 就有反馈），配置驱动 + 空正文自愈。

    自愈链：配置档位 → 安全档（think=low + 更大预算）→ fallback_model。
    推理模型（gpt-oss）即使 think=false 也会先生成思考 token，可能整轮
    没有任何 response —— 若发生，这里自动重试，上层不会拿到空串。
    """
    p = _profile()
    st = {}
    for acc in _stream_once(prompt, OLLAMA_MODEL, p["think"], p["num_predict"],
                            p["temperature"], timeout, st, p.get("num_ctx")):
        yield acc
    if st.get("chars"):
        return

    print(f"[DSH] ⚠️ {OLLAMA_MODEL} 流式首轮无正文（思考 {st.get('thinking', 0)} 字），安全档重试")
    st2 = {}
    for acc in _stream_once(prompt, OLLAMA_MODEL, _SAFE_RETRY["think"],
                            _SAFE_RETRY["num_predict"], p["temperature"], timeout, st2,
                            p.get("num_ctx")):
        yield acc
    if st2.get("chars"):
        return

    fb = LLM_CFG.get("fallback_model")
    if fb and fb != OLLAMA_MODEL:
        print(f"[DSH] ⚠️ 安全档仍无正文，回退 {fb}")
        fp = _profile(fb)
        st3 = {}
        for acc in _stream_once(prompt, fb, fp["think"], fp["num_predict"],
                                fp["temperature"], timeout, st3):
            yield acc


def _default_image_model() -> str:
    """config.json 里配的生图默认模型（stages.image.default_model）。

    ★★ 为什么不能在这里偷懒写死 "z_image"（2026-10-15 的事故）★★
      这个值是填进**给 agent 的提示词**里的（下面 "选用模型：{model}" 那一行），
      agent 看到之后就会在自己的回复里说"我打算用 z_image 出图"。
      也就是说：**显示、agent 的决策、实际执行会被这个值一起带走**。
      写一个和 config 不一致的默认值 = 三方各说各话。

      实测事故：用户要浣熊 → 图也对（qwen21_ref 出的）→ 之后某轮
      这里退回写死的 z_image → z_image 当前**完全不认提示词**（要浣熊出小男孩、
      要大象也出小男孩）→ 建模建出来的就是个小男孩。用户看到"图片对、建模跑题"。

    ★ 为什么不直接 import conversation._pick_image_model：
      conversation 已经 import 了本模块（enhance_image_prompt），反向 import 会成环。
      这里只读同一个字段，保证"显示的和实际用的是同一个模型"。
    """
    try:
        cfg_path = Path(__file__).resolve().parent.parent / "config.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        m = (cfg.get("stages", {}).get("image", {}).get("default_model") or "").strip()
        if m:
            return m
    except Exception:
        pass
    return "z_image"


def _summary_prompt(notes: list, params: dict, latest: str = "") -> str:
    """需求聊天用的总结 prompt：把用户**所有轮次**的话交给 LLM 做综合。

    2026-09-22：宇凯要「聊天式定需求」——一组对话持续累积上下文，
    AI 每轮给一份总结，用户看总结决定「生成图片」还是继续聊。
    """
    notes = [n for n in (notes or []) if n] or [latest]
    req = params.get("requirement") or latest or "（暂无）"
    hist = "\n".join(f"  {i}. {n}" for i, n in enumerate(notes, 1))
    style = params.get("style", "未指定")
    size = params.get("size_mm", "未指定")
    color = params.get("color", "未指定")
    model = params.get("model") or _default_image_model()
    return f"""用户在这组对话里**分多轮**逐步提出的需求（按时间顺序）：
{hist}

综合以上所有轮次后的完整需求：{req}
已自动判断的参数：
  - 风格：{style}
  - 打印尺寸：{size}mm
  - 颜色：{color}
  - 选用模型：{model}

请用中文回复，直接说，不要分点、不要标题，共 2-3 段、总计 220 字以内：

第一段：以「我目前理解的需求是…」开头，把用户**所有轮次**说的话综合成一段完整描述。
要求体现你记得前面提过的细节（比如先说的主体、后补的颜色/尺寸/结构），不要只回应当前这一句。

第二段：说明打算用哪个模型生成、它擅长什么、预期效果；末尾用一句话列出已确定的参数（风格/尺寸/颜色）。

第三段：用一句话问用户是否需要补充或调整——满意就点「🎨 生成图片」，想继续聊就直接说。

语气：专业、耐心、有温度，像热情的 3D 打印顾问。不要复述用户原句。"""


def generate_confirm_response_stream(user_input: str, understood: str, auto_params: dict):
    """需求阶段的流式总结回复：逐块 yield 累积文本（每轮都调用，累积上下文）。"""
    if not _is_ollama_alive():
        yield _fallback_confirm_response(user_input, understood, auto_params)
        return

    prompt = _summary_prompt(auto_params.get("notes") or [user_input],
                             auto_params, user_input)

    got = ""
    try:
        for acc in _stream_ollama(prompt):
            got = acc
            yield acc
    except Exception:
        pass
    if not got:
        yield _fallback_confirm_response(user_input, understood, auto_params)


def _is_ollama_alive() -> bool:
    try:
        import urllib.request
        urllib.request.urlopen(f"{OLLAMA_URL}/api/tags", timeout=3)
        return True
    except Exception:
        return False


# ─── 图片描述 ────────────────────────────────────────────────────

def describe_image(image_path: str, user_context: str = "") -> str:
    """为图片生成详细中文描述"""
    if not _is_ollama_alive():
        return "【图片描述】这是一张 AI 生成的图片。"

    # 构建提示词
    context_hint = f"\n用户想要的是：{user_context}" if user_context else ""

    prompt = f"""你是一个专业的美术描述师。请详细描述这张图片的内容。

要求：
- 先说明图片的主体是什么（人物/动物/物品/场景）
- 描述主体的大小、比例、颜色、材质、细节特征
- 说明背景是什么，色调如何
- 指出特别值得注意的细节（表情、姿势、服饰、纹路等）
- 判断这张图是否适合作为3D打印的参考图（背景干净、主体突出、轮廓清晰则更适合）
- 如果描述的是卡通角色，要说明是哪个作品里的哪个角色{context_hint}

请用中文，200字以内，条理清晰。"""

    return _call_ollama(prompt)


def describe_image_with_style(image_path: str, style: str, user_context: str = "") -> str:
    """为指定风格的图片生成描述"""
    if not _is_ollama_alive():
        return f"【图片描述】这是一张{style}风格的 AI 生成图片。"

    prompt = f"""这是一张{style}风格的图片。{f'用户想要的是：{user_context}' if user_context else ''}

请详细描述：
- 图片主体是什么，{style}风格下呈现的外观特征
- 主体的大小、比例、颜色、材质、质感
- 背景和整体色调
- 特别值得注意的细节

请用中文，200字以内。"""

    return _call_ollama(prompt)


def describe_3d_model(model_path: str, user_context: str = "") -> str:
    """为3D模型生成描述"""
    if not _is_ollama_alive():
        return "【3D模型】这是一个 3D 模型文件。"

    prompt = f"""你是一个 3D 打印专家。请描述这个 3D 模型的特点。

{f'用户最初想要的是：{user_context}' if user_context else ''}

请从以下角度描述：
- 模型的主体形状（高大/扁平/圆润/方正/不规则）
- 整体比例和大小感
- 表面细节（光滑/有纹理/有镂空/有凹凸）
- 颜色（如有贴图，描述贴图风格）
- 这个模型适合哪种打印方向（竖直/水平/需要支撑）
- 预估支撑需求（底部面积大则支撑少，悬空结构多则需要支撑）
- 建议的打印层高（如有精细结构用 0.12mm，普通用 0.2mm）

请用中文，250字以内。"""

    return _call_ollama(prompt)


# ─── 对话回复生成（用 LLM 做） ──────────────────────────────────

def generate_confirm_response(user_input: str, understood: str, auto_params: dict) -> str:
    """需求阶段（非流式）的综合总结：把多轮上下文交给 LLM 汇总。"""
    if not _is_ollama_alive():
        return _fallback_confirm_response(user_input, understood, auto_params)

    prompt = _summary_prompt(auto_params.get("notes") or [user_input],
                             auto_params, user_input)
    return _call_ollama(prompt, timeout=60)


def generate_3d_progress_response(model_info: dict) -> str:
    """阶段 3：生成3D时的描述性回复"""
    model_name = model_info.get("name", "TripoSR v2")
    user_context = model_info.get("user_context", "")

    if not _is_ollama_alive():
        return _fallback_3d_progress(user_context)

    prompt = f"""用户选择了这张图片，正在进行 3D 建模。
用户想要的是：{user_context}

请用中文回复用户，格式如下：

我正在使用 {model_name} 模型为你进行 3D 重建。

{model_name} 是一款基于单张图片的高质量 3D 重建模型。它的原理是：
- 首先从输入图片中提取物体的几何特征和深度信息
- 通过多视角一致性约束，在 3D 空间中还原物体的形状
- 最后生成带纹理的无缝网格模型，可以直接导出 STL 进行 3D 打印

预计耗时 30-60 秒，请稍候...

（生成完毕后，我会展示模型的多个角度预览，并告诉你最适合的打印方向和支撑需求）"""

    return _call_ollama(prompt, timeout=40)


def _fallback_confirm_response(user_input: str, understood: str, params: dict) -> str:
    """无 LLM 时的降级回复"""
    style = params.get("style", "写实")
    size = params.get("size_mm", 120)
    model = params.get("model", "FLUX.1")

    return f"""好的，我来帮你生成「{understood}」。

我理解你想要的是：{user_input}

我打算使用 {model} 模型以 **{style}** 风格生成图片，
打印尺寸预设约 {size}mm。

参数确认：
  • 风格：{style}
  • 尺寸：{size}mm
  • 模型：{model}

以上理解正确吗？有什么需要调整的？"""



def _fallback_3d_progress(user_context: str) -> str:
    return f"""正在使用 TripoSR v2 模型为你进行 3D 重建。

TripoSR 是一款基于单张图片的 3D 重建模型，原理是：
1. 从图片中提取几何特征和深度信息
2. 通过多视角一致性约束还原 3D 形状
3. 生成可 3D 打印的无缝网格模型

预计耗时 30-60 秒，请稍候..."""
