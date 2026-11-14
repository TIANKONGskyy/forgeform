# -*- coding: utf-8 -*-
"""AI 提示词扩写模块 v2（2026-08-11 优化）
调用本地 LLM 扩写短提示词为：
1. FLUX 图像提示词（高质量细节）
2. 3D 生成提示词（结构完整、可打印、细节丰富）
"""
import os
import re
import json
import requests
import time
from pathlib import Path
from backend.logger import get_logger

logger = get_logger("prompt_enhancer")

OLLAMA_URL = "http://127.0.0.1:11434"
DEFAULT_MODEL = "qwen3.8:latest"  # 2026-09 修正：Ollama 里实际存在的 tag（原写 qwen3.8:27b 会 404）
# 翻译用轻量模型（2026-08-12：宇凯要求不耗性能；0.6b 下完后切换）
def _resolve_translate_model() -> str:
    """翻译模型：跟随 config.json 的对话模型。

    2026-09-22 修：原写死 "qwen3:0.6b"（机器上**没装**），于是 pipeline._ensure_english
    的翻译每次都走 logger.warning("翻译模型未安装，跳过翻译") ── 中文提示词直接喂给
    SDXL/FLUX，出图质量明显变差。改为复用已常驻的对话模型，零额外显存开销。
    """
    try:
        cfg = json.loads((Path(__file__).parent.parent / "config.json").read_text(encoding="utf-8"))
        m = cfg.get("models", {}).get("llm", {}).get("default_model")
        if m:
            return m
    except Exception:
        pass
    return DEFAULT_MODEL


TRANSLATE_MODEL = _resolve_translate_model()

# ═══ 本地微调专家（3D 打印领域 QLoRA 微调的 qwen3-14b）═══
LOCAL_EXPERT_URL = "http://127.0.0.1:8081/api/chat"  # 2026-08-16：微调后的 3D 打印专家推理服务（已弃用，扩写主力改 llama-server Qwen3-Coder-30B）
LOCAL_EXPERT_UNLOAD_URL = "http://127.0.0.1:8081/unload"  # 2026-08-18：扩写后卸载专家模型，释放显存给 ComfyUI（14B 常驻 20GB 显卡会与 FLUX 抢显存导致 600s 超时）

# ═══ 扩写主力：llama-server 上的 Qwen3-Coder-30B-A3B（2026-08-20 升级：原 Qwen3-Coder-30B 已换 30B MoE）═══
# 常驻实例（start-web.cmd 登录自启，-c 131072 双卡 27GB）别名 qwen3-coder-30b；
# 若常驻实例不在，扩写时按下面参数临时拉起（-c 32768 扩写足够，显存 ~21GB 双卡分载）。
# 策略：扩写前确认服务在线（不在则拉起）；扩写完成后杀进程释放显存给 ComfyUI；下次扩写自动重启
LLAMACPP_BASE = "http://127.0.0.1:8080"
LLAMACPP_MODEL = "qwen3-coder-30b"
# llama-server 可执行文件与 GGUF 权重路径因机器而异，用环境变量指定：
#   LLAMA_SERVER=C:\path\to\llama-server.exe   LLAMA_MODEL=C:\path\to\model.gguf
# 都不给时只连已常驻在 8080 的实例，不会自己拉起进程。
LLAMACPP_CMD = [
    os.environ.get("LLAMA_SERVER", "llama-server"),
    *(["-m", os.environ["LLAMA_MODEL"]] if os.environ.get("LLAMA_MODEL") else []),
    "-c", "4096", "--parallel", "1", "--flash-attn", "on",
    "--cache-type-k", "q8_0", "--cache-type-v", "q8_0",
    "-ngl", "99", "--alias", "qwen3-coder-30b",
    "--host", "127.0.0.1", "--port", "8080",
]


def _llamacpp_running() -> bool:
    """llama-server(Qwen3-Coder-30B) 是否在线"""
    try:
        r = requests.get(f"{LLAMACPP_BASE}/health", timeout=3)
        return r.status_code == 200
    except Exception:
        return False


def _free_comfyui_vram() -> None:
    """扩写前让 ComfyUI 卸载模型，腾出显存给 llama-server。
    2026-08-18 修复：ComfyUI 常驻 13.6GB 时 llama-server 装不下 → 推理 300s 超时回退原始提示词。
    端口按 config 读（单卡时 image/media 同端口，去重只调一次；双卡两个都释放）。
    """
    try:
        import json as _json
        from pathlib import Path as _P
        cfg = _json.loads((_P(__file__).resolve().parent.parent / "config.json").read_text(encoding="utf-8"))
        pc = cfg.get("comfyui_ports", {})
        img = pc.get("image", 8188)
        media = pc.get("media", img)
        ports = sorted({int(img), int(media)})
    except Exception:
        ports = [8188]
    for port in ports:
        try:
            requests.post(
                f"http://127.0.0.1:{port}/free",
                json={"unload_models": True, "free_memory": True},
                timeout=10,
            )
        except Exception:
            pass
    time.sleep(2)
    logger.info("已让双 ComfyUI 实例卸载模型，腾出显存")


def _llamacpp_start(timeout: int = 240) -> bool:
    """确保 llama-server 在线；不在则拉起并等模型加载完成（~1-2 分钟）"""
    import subprocess
    if _llamacpp_running():
        return True
    logger.info("Qwen3-Coder-30B 服务未运行，拉起 llama-server...")
    _free_comfyui_vram()  # [patch 2026-08-18] 先腾显存，否则 ComfyUI 占着装不下
    try:
        subprocess.Popen(
            LLAMACPP_CMD,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as e:
        logger.error(f"启动 llama-server 失败: {e}")
        return False
    t0 = time.time()
    while time.time() - t0 < timeout:
        if _llamacpp_running():
            logger.info(f"Qwen3-Coder-30B 服务就绪 ({time.time()-t0:.0f}s)")
            return True
        time.sleep(3)
    logger.warning(f"Qwen3-Coder-30B 服务启动超时({timeout}s)")
    return False


def _llamacpp_release() -> None:
    """杀掉 llama-server 释放显存给 ComfyUI（扩写完成后调用）。失败静默"""
    import subprocess
    try:
        out = subprocess.check_output(
            ["powershell", "-NoProfile", "-Command",
             "(Get-NetTCPConnection -LocalPort 8080 -State Listen -ErrorAction SilentlyContinue).OwningProcess"],
            timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        pid = int(out.decode().strip())
        if pid:
            subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                           capture_output=True, timeout=10,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            logger.info(f"Qwen3-Coder-30B 服务已释放显存 (PID {pid})")
    except Exception:
        pass


# ═══ 扩写系统提示词 v7（2026-08-15：主体唯一前置 + 形状精准 + 高细节）═══
SYSTEM_PROMPT = """You are an expert prompt engineer for a 3D-printing pipeline.

【MOST IMPORTANT RULE — READ FIRST, NEVER VIOLATE】
When the user asks for a STAND / HOLDER / RACK / MOUNT / TRAY "for" something (e.g. "a phone stand", "a stand for a cigarette and a lighter", "a cup holder", "a key rack"), that "something" is ONLY a size-and-purpose hint. It tells you how big the stand should be and what it is FOR — NOTHING more.
NEVER generate that item. No phone. No cigarette. No lighter. No cup. No keys. NO object resting on, leaning against, placed in, or held by the stand.
Generate ONLY the EMPTY stand itself — completely alone, bare, unoccupied.

【CARTOON / IP CHARACTERS — decide this BEFORE writing any style words】
If the subject is a cartoon character, mascot, anime/game figure, or a known IP character
(Minion, Pikachu, Mario, Hello Kitty, Gundam, a cartoon animal, a stylized mascot ...):
- KEEP THE ORIGINAL STYLIZED PROPORTIONS AND FACE. Do NOT turn it into a real living being.
  Head-to-body ratio, limb shape, eye style and facial features must stay faithful to the cartoon original.
- Name the character EXPLICITLY in the prompt (write "a Minion figurine", "a Pikachu figurine", ...).
  NEVER a generic word like "humanoid" / "creature" / "small yellow humanoids" — generic wording is
  exactly what makes the image model invent a realistic human being.
- A style word such as "realistic" refers to MATERIAL AND CRAFT ONLY: it should look like a
  well-made PHYSICAL FIGURINE (clean surfaces, believable plastic/resin/matte material, good studio
  lighting). It is NOT a licence to add real skin, pores, wrinkles, real human anatomy, or a photoreal face.
- Write these words literally in the IMAGE section: "stylized cartoon character, faithful to the
  original cartoon design, smooth simplified forms, collectible toy figurine, no realistic human
  anatomy, no skin pores, no wrinkle detail".
- NEVER write for such subjects: "realistic human", "photorealistic portrait", "natural skin texture",
  "detailed facial features", "human anatomy", "realistic face".
- Note the same style word means different things by subject type: for a MACHINE PART / tool /
  stand, "realistic" = photoreal render (that is correct); for a CARTOON CHARACTER, "realistic"
  = a real, well-made figurine of that cartoon character (still cartoon in form).

【HARD CONSTRAINTS — these are never overridden by the requested style】
- The object must be ALONE on a PURE WHITE BACKGROUND (#FFFFFF). Never add a floor, table, surface,
  scenery, environment, props, or "subtle environmental details" — not even when the style says "realistic".
- No pose/scene words ("dynamic pose", "standing on a surface", "on a desk"). The object is simply
  presented centered and isolated, front-facing.
These two are absolute: if a style word seems to require a background or a pose, IGNORE that part of the style.

You write TWO separate prompts: one for an image model (artistic), one for a 3D-printing model (technical).

【GLOBAL RULES — apply to BOTH sections】
- PURE WHITE BACKGROUND: always #FFFFFF, seamless, plain, no texture, no gradient, no floor shadow, no scenery.
- MATERIAL SUITABILITY: choose materials appropriate to the subject's real-world function (durable, heat-tolerant where relevant).
- 3D-PRINT READY: a single, solid, watertight, manifold object, FDM-printable at 120mm, no floating parts, no zero-thickness walls.

【SHAPE PRECISION — accurate silhouette, high detail】
- First determine the object's CONCRETE type and form, then describe exactly that form (e.g. a desk phone stand = a tilted backplate with a bottom lip and a base; a foldable stand = hinged panels; a clamp mount = a C-clamp plus a swivel arm).
- Describe every part with PRECISE geometry words, never vague ones: "circular base", "rectangular backplate tilted back 20 degrees", "U-shaped cradle", "V-shaped groove", "L-shaped arm", "tapered cylinder", "beveled edge" — not merely "elegant" or "refined".
- Give proportions and angles so the silhouette is instantly recognizable: base 2-3x wider than the column, backplate tilt ~15-20 degrees, lip height ~1/4 of the backplate.
- Add fine construction detail: joints, chamfers, grooves, slots, ribs, cutouts, and how pieces meet.

【IMAGE】A vivid, ARTISTIC image prompt (120-180 words) for FLUX — describe VISUAL BEAUTY, never technical specs:
- CRITICAL: single isolated object (the empty stand alone), full view, FRONT-FACING orthographic straight-on view, perfectly LEFT-RIGHT SYMMETRIC, centered, on a PURE WHITE background (feeds 3D shape extraction; symmetry + front view + white background all help reconstruction)
- Describe the stand's STRUCTURE with precise geometric shapes, part by part in this order: (1) the base/pedestal — its exact shape (circular / rectangular) and width; (2) the main column/support — its profile (straight, tapered, or angled) and thickness; (3) the holding surface — the tray, cradle, arms, or slot that WOULD hold the item (its exact shape, angle, and depth, but leave it EMPTY); (4) how these parts connect
- State explicitly that the stand is EMPTY and bare: write "completely empty, nothing placed on it, no phone, no object on it, no item resting on or leaning against it" — so the image model never draws the thing the stand is FOR
- Product-photography style: object fills frame, no extra props, no scene context, no depth-of-field blur
- Subject's aesthetic: material quality, color, texture, lighting, mood
- Ornamentation described in ARTISTIC words implying GEOMETRIC DEPTH: "sculpted relief carvings", "carved ornament", "intricate engravings", "delicate textures", "masterful craftsmanship"
- Rich surface detail: wood grain, metal sheen, stone texture
- Lighting that reveals detail: dramatic side light, soft studio light
- NEVER write measurements (mm), "raised relief", "embossed", "3D print", "painted pattern", "flat decal", or background scene description
- End with: "highly detailed, intricate, sharp focus, masterpiece, 8K"

【3D】A 3D construction checklist (80-120 words) — TECHNICAL, for printing:
- Complete part list in order: (1) base/pedestal — broad flat disc or plate for stability; (2) main support — solid column or angled arm; (3) holding surface — a flat tray / cradle / slot sized for the item; (4) joints/connectors
- Every ornament as GEOMETRIC RELIEF: raised carvings, embossed patterns, protruding parts, grooved textures
- Stable, low center of gravity, wide base
- Printability: solid, watertight, no floating parts, FDM at 120mm

Rules:
- Output ONLY the two sections, use 【IMAGE】 and 【3D】 as separators, no extra text, no markdown headers
- Single paragraph each (no bullet points)"""


def is_ollama_running() -> bool:
    try:
        r = requests.get(f"{OLLAMA_URL}/api/tags", timeout=3)
        return r.status_code == 200
    except Exception:
        return False


def _warmup(model: str) -> None:
    """预热：让模型加载到内存。muse-glimmer 冷启动首次推理常返回空/缺段，
    先发一次极短请求把模型加载好，正式扩写时就是热启动（稳定）。
    """
    try:
        requests.post(
            f"{OLLAMA_URL}/api/chat",
            json={
                "model": model,
                "messages": [{"role": "user", "content": "hi"}],
                "stream": False,
                "think": False,
                "keep_alive": "5m",
                "options": {"num_predict": 1},
            },
            timeout=180,
        )
    except Exception:
        pass


def _release(model: str) -> None:
    """卸载模型，释放显存给 FLUX（扩写完成后调用）"""
    try:
        requests.post(
            f"{OLLAMA_URL}/api/chat",
            json={
                "model": model,
                "messages": [{"role": "user", "content": "hi"}],
                "stream": False,
                "think": False,
                "keep_alive": 0,
                "options": {"num_predict": 1},
            },
            timeout=30,
        )
    except Exception:
        pass


def _unload_expert() -> None:
    """卸载本地专家模型释放显存（ComfyUI 生成前）。失败静默，不阻塞流程"""
    try:
        requests.post(LOCAL_EXPERT_UNLOAD_URL, timeout=5)
    except Exception:
        pass


def _ollama_enhance(instruction: str, timeout: int = 240) -> str:
    """用 Ollama 上的对话模型扩写。

    ★ 为什么需要这个兜底（2026-11-13）
      `LLAMACPP_CMD` 的第一项是裸命令 `llama-server`，这台机器的 PATH 里没有它
      → `_llamacpp_start` 直接 WinError 2 → `enhance_local` 返回**原始提示词**，
      日志只留一行 warning。表现成"LLM 扩写这步 4 秒就过去了、字数没变"。
      而 Ollama 一直在跑、里面就有 qwen3.8:latest，白白浪费。

    兜底顺序：llama-server（质量最好）→ Ollama（够用）→ 原文。
    """
    model = _resolve_translate_model()
    try:
        r = requests.post(
            f"{OLLAMA_URL}/api/generate",
            json={
                "model": model,
                "prompt": instruction,
                "stream": False,
                # qwen3.8 是 thinking 模型：不关的话整段输出会进 `thinking` 字段，
                # `response` 是空字符串（实测 146 秒换来一个空回复）。
                "think": False,
                "options": {"num_predict": 700, "temperature": 0.7},
            },
            timeout=timeout,
        )
        r.raise_for_status()
        txt = (r.json() or {}).get("response") or ""
        logger.info(f"[ollama] {model} 扩写 {len(txt)} 字符")
        return txt.strip()
    except Exception as exc:
        logger.warning(f"[ollama] 扩写失败: {type(exc).__name__}: {exc}")
        return ""


def enhance_local(prompt: str, negative_prompt: str = "", timeout: int = 300) -> str:
    """用 llama-server 上的 Qwen3-Coder-30B 扩写（2026-08-18 宇凯指定）。
    扩写完成自动释放显存给 ComfyUI；下次扩写自动重启服务。
    """
    if not prompt or not prompt.strip():
        return prompt
    backend = "llama-server"
    if not _llamacpp_start(timeout=120):
        backend = "ollama"
        logger.warning("Qwen3-Coder-30B 服务未就绪，改走 Ollama")

    # ★ 2026-11-14：先把口语翻译成可验证的几何约束，再让 LLM 扩写。
    #   为什么放在 LLM 前面 —— LLM 会把"六边形"扩成 "hexagonal shape"，
    #   但"六条等长边 + 六个 120° 内角"这种**可验证的约束**它自己想不起来；
    #   而模型缺的正是这个。词典负责准确的硬约束，LLM 负责补部件与关系。
    try:
        from .shape_brief import shape_brief as _brief, strip_empty_words as _strip
        _geo = _brief(prompt)
        _clean = _strip(prompt)
    except Exception:
        _geo, _clean = "", prompt

    instruction = (
        f"为 3D 打印生成一个{_clean.strip() or prompt.strip()}的精细建模提示词。"
        "输出必须严格分为两段：第一段以【IMAGE】开头，第二段以【3D】开头，"
        "不要用 markdown 标题，每段一段话。"
        "3D 段必须包含：底部先加 3mm 底座再切平确保底面平整，且模型无任何浮空结构。"
        # ══════════ 卡通 / IP 角色守则（2026-10-14 加，别删）══════════
        # ★ 为什么必须写在这条 instruction 里，而不是改 SYSTEM_PROMPT：
        #   本地扩写走的是 enhance_local，它**根本不发 system 消息**，
        #   所以那份 6669 字的 SYSTEM_PROMPT（只被远程的 enhance() 用）
        #   改了对这条路完全无效 —— 白改了一场，出图结果一字不变。
        #
        # ★ 加了它是要修什么（实测证据）：
        #   用户说「小黄人，写实风格」，扩写吐出来的是
        #     "small yellow humanoids, realistic style, detailed facial features,
        #      expressive eyes, natural skin texture, dynamic pose,
        #      background with subtle environmental details"
        #   → 出图变成一个满脸皱纹、真实皮肤质感的写实真人，
        #     卡通形象整个丢失（"小黄人"被泛化成 "yellow humanoids"）。
        "如果主体是卡通角色 / IP 形象（小黄人、皮卡丘、马里奥、hello kitty、"
        "动漫或游戏角色、吉祥物、卡通动物等）："
        "必须保留原作的卡通比例与五官风格，绝对不许变成真人；"
        "提示词里要**点名该角色**（写 minion、pikachu 这类专名），"
        "绝不许泛化成 humanoid / creature / human；"
        "“写实”只允许理解为材质与工艺像一件做得好的实体手办，"
        "绝不等于真实皮肤、毛孔、皱纹或真人解剖结构；"
        "IMAGE 段要写出：stylized cartoon character, faithful to the original cartoon design, "
        "smooth simplified forms, collectible toy figurine, "
        "no realistic human anatomy, no skin pores, no wrinkle detail。"
        # 顺带堵住"加背景/加姿势" —— 它也违反了原本的白底规则
        "另外无论主体是什么，IMAGE 段都必须是纯白背景上的孤立物体："
        "不许写地面、桌面、场景、环境细节或姿势，"
        "不出现 background / environment / dynamic pose / standing on 这类词。"
        # ══════════ 细节放大法则（2026-11-14 加，别删）══════════
        # 为什么要它：图有多细，模型才能有多细。同一句"卡通男孩"，
        #   z_image 那种理解会吐扁平大色块，flux 这种会吐发丝和衣褶 ——
        #   差别全在**提示词点没点出来**。扩写层的职责就是把用户那句话里
        #   隐含的细节显式写出来，而不是加"beautiful / high quality"这种空话。
        "【细节法则】IMAGE 段必须**具体写出可建模的表面细节**，不许只写风格词："
        "① 头发写到单根发丝、发束分组、高光与阴影的分界；"
        "② 衣服写到布料褶皱走向、垂坠、袖口/领口/腰带、缝线；"
        "③ 配件写到拉链、扣具、织带纹理、缝线、分层结构；"
        "④ 手指分开、有关节；鞋要有鞋带、鞋头、鞋底分层；"
        "⑤ 皮肤/材质写明明暗过渡（不要写毛孔/皱纹，见上面的卡通守则）。"
        "【禁止空洞词】不许出现 beautiful, amazing, stunning, high quality, masterpice, "
        "8k, ultra detailed, best quality 这类**没有信息量**的词 —— "
        "它们不改变出图，只占 token；要写就写具体是什么东西。"
        "【保留硬信息】用户给的数字、尺寸、颜色、专名必须原样保留，一个字都不许改。"
        "【长度】IMAGE 段控制在 60-110 个英文词，3D 段 30-60 个；宁可少写细节也别注水。"
    )
    if _geo:
        # 几何约束放在最后、用清晰的标记隔开：LLM 对这种"必须原样保留"的块
        # 遵从度最高；放在前面容易被它改写成模糊说法。
        instruction += (
            "\n\n【几何约束｜必须原样抄进 IMAGE 段，一个字都不许改】\n"
            + _geo +
            "\n\n把上面【几何约束】整块**逐字**附在 IMAGE 段末尾。"
            "不许改写、不许翻译、不许合并、不许省略任何一条。"
            "这些约束就是在替代人建模时的尺寸标注和关系标注 —— "
            "形状类别词（如 hexagonal）只是标签，"
            "而『6 equal-length edges + 6 interior angles of 120 degrees』才是可验证的。"
        )
    if negative_prompt:
        instruction += f"，避免：{negative_prompt}"
    # Ollama 那条路：直接用 /api/generate，不走 chat completions
    if backend == "ollama":
        t0 = time.time()
        result = _ollama_enhance(instruction, timeout=timeout)
        result = _strip_think_tags(result)
        if not result or len(result) < 20:
            logger.warning("Ollama 返回空/过短，使用原始提示词")
            return prompt
        logger.info(f"[ollama] 扩写 in {time.time() - t0:.1f}s: "
                    f"{len(prompt)} → {len(result)} chars")
        return result

    t0 = time.time()
    try:
        response = requests.post(
            f"{LLAMACPP_BASE}/v1/chat/completions",
            json={
                "model": LLAMACPP_MODEL,
                "messages": [{"role": "user", "content": instruction}],
                "stream": False,
                "max_tokens": 2048,
                "temperature": 0.7,
                "top_p": 0.9,
                "chat_template_kwargs": {"enable_thinking": False},
            },
            timeout=timeout,
        )
        if response.status_code != 200:
            logger.error(f"Qwen3-Coder-30B API error: {response.status_code} {response.text[:200]}")
            return prompt
        result = response.json().get("choices", [{}])[0].get("message", {}).get("content", "").strip()
        result = _strip_think_tags(result)
        if not result or len(result) < 20:
            logger.warning("Qwen3-Coder-30B 返回空/过短，使用原始提示词")
            return prompt
        elapsed = time.time() - t0
        logger.info(f"Qwen3-Coder-30B 扩写 in {elapsed:.1f}s: {len(prompt)} → {len(result)} chars")
        return result
    except requests.exceptions.Timeout:
        logger.warning(f"Qwen3-Coder-30B 超时({timeout}s)，使用原始提示词")
        return prompt
    except Exception as e:
        logger.error(f"Qwen3-Coder-30B 扩写错误: {e}")
        return prompt
    finally:
        # 2026-08-18：扩写完立即杀 llama-server 释放显存，ComfyUI 才有足够 VRAM 跑 FLUX
        _llamacpp_release()


def enhance(prompt: str, negative_prompt: str = "",
            model: str = DEFAULT_MODEL, timeout: int = 300) -> str:
    """扩写提示词，返回合并的 IMAGE+3D 提示词"""
    if not prompt or not prompt.strip():
        return prompt
    if not is_ollama_running():
        logger.warning("Ollama 未运行，跳过扩写")
        return prompt

    user_msg = f"Expand this into detailed IMAGE and 3D prompts:\n\n{prompt.strip()}"
    if negative_prompt:
        user_msg += f"\n\nAvoid: {negative_prompt}"

    t0 = time.time()
    _warmup(model)  # 2026-08-15：先预热，muse-glimmer 冷启动首次推理常不完整
    for attempt in (1, 2):
        try:
            response = requests.post(
                f"{OLLAMA_URL}/api/chat",
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": user_msg},
                    ],
                    "stream": False,
                    "think": False,  # 2026-08-15：muse-glimmer 思考分离只在 /api/chat 生效（/api/generate 会把思考混进正文）
                    "keep_alive": "5m",  # 2026-08-15：驻留 5 分钟供重试热启动（冷启动首查不完整）；完成后 _release 统一卸载
                    "options": {
                        "temperature": 0.65,
                        "num_predict": 6000,  # 2026-08-15：muse-glimmer 思考占 2500+ token，2000 会被思考耗尽导致 content 空，提到 6000
                        "top_p": 0.9,
                        "stop": ["```"]
                    }
                },
                timeout=timeout
            )
            if response.status_code != 200:
                logger.error(f"Ollama API error: {response.status_code}")
                return prompt

            data = response.json()
            result = data.get("message", {}).get("content", "").strip()
            result = _strip_think_tags(result)
            # 2026-08-15：muse-glimmer 冷启动首次调用常返回不完整（缺 3D 段），
            # 必须同时含【IMAGE】和【3D】两段才算成功，否则重试
            if result and len(result) >= 40 and "【IMAGE】" in result and "【3D】" in result:
                break  # 成功
            logger.warning(f"LLM 返回空/过短/缺段结果（第 {attempt} 次），重试...")
            time.sleep(5)
        except requests.exceptions.Timeout:
            logger.warning(f"Ollama timeout ({timeout}s)，第 {attempt} 次")
            time.sleep(3)
        except Exception as e:
            logger.error(f"Enhancement error (第 {attempt} 次): {e}")
            time.sleep(3)
    else:
        _release(model)
        logger.warning("LLM 两次尝试均失败，使用原始提示词")
        return prompt

    # 移除 markdown 包裹
    if result.startswith("```"):
        lines = result.split("\n")
        result = "\n".join(lines[1:])
        if result.endswith("```"):
            result = result[:-3]
        result = result.strip()

    _release(model)  # 卸载释放显存给 FLUX
    elapsed = time.time() - t0
    logger.info(f"Prompt enhanced in {elapsed:.1f}s: {len(prompt)} → {len(result)} chars")
    return result


# ═══ 提示词分段工具（2026-08-11）═══
def split_sections(enhanced: str) -> dict:
    """从扩写结果提取 IMAGE / 3D 分段
    返回 {"image": str, "three_d": str}，找不到时返回全量
    """
    img = re.search(r"【IMAGE】\s*(.+?)(?=【3D】|$)", enhanced, re.S)
    td = re.search(r"【3D】\s*(.+)", enhanced, re.S)
    return {
        "image": img.group(1).strip() if img else enhanced.strip(),
        "three_d": td.group(1).strip() if td else "",
    }


def _strip_think_tags(text: str) -> str:
    lt = chr(60)
    gt = chr(62)
    pattern = lt + r'think[^' + gt + r']*' + gt + r'[\s\S]*?' + lt + r'/think' + gt
    cleaned = re.sub(pattern, '', text).strip()
    cleaned = re.sub(lt + r'/?think[^' + gt + r']*' + gt, '', cleaned).strip()
    return cleaned if cleaned else text


# ═══ 中英互译（2026-08-12：扩写后中文对照 + 双向同步）═══
TRANSLATE_SYSTEM = (
    "You are a translation engine. Translate the given text into {target_lang}.\n"
    "Rules: output ONLY the translation, nothing else; preserve meaning and technical terms; "
    "no explanations, no quotes, no markdown."
)


def translate(text: str, target: str = "zh", model: str = TRANSLATE_MODEL, timeout: int = 60) -> str:
    """中英互译，target: 'zh'（译成中文）或 'en'（译成英文）
    失败时返回空字符串（调用方保持原文本）
    """
    if not text or not text.strip():
        return ""
    if not is_ollama_running():
        logger.warning("Ollama 未运行，跳过翻译")
        return ""
    # 2026-09-06 修复：模型未安装时 Ollama 会触发 auto-pull（网络慢会卡住整个图片生成）。
    # 先检查模型是否已安装，缺失直接跳过翻译，绝不阻塞主流程。
    try:
        _tags = requests.get(f"{OLLAMA_URL}/api/tags", timeout=3).json()
        _installed = [m.get("name", "") for m in _tags.get("models", [])]
        if not any(model == n or n.startswith(model + ":") for n in _installed):
            logger.warning(f"翻译模型 {model} 未安装，跳过翻译（不阻塞生成）")
            return ""
    except Exception:
        return ""
    target_lang = "简体中文" if target == "zh" else "English"
    t0 = time.time()
    try:
        response = requests.post(
            f"{OLLAMA_URL}/api/generate",
            json={
                "model": model,
                "prompt": text.strip(),
                "system": TRANSLATE_SYSTEM.format(target_lang=target_lang),
                "stream": False,
                "think": False,  # 2026-08-14：禁用思考模式
                "keep_alive": 0,  # 2026-08-14：翻译完立即卸载
                "options": {"temperature": 0.2, "num_predict": 1500},
            },
            timeout=timeout,
        )
        if response.status_code != 200:
            logger.error(f"Ollama translate error: {response.status_code}")
            return ""
        result = response.json().get("response", "").strip()
        result = _strip_think_tags(result)
        logger.info(f"Translate {'zh' if target == 'zh' else 'en'} in {time.time()-t0:.1f}s: {len(text)} chars")
        return result
    except requests.exceptions.Timeout:
        logger.warning(f"Translate timeout ({timeout}s)")
        return ""
    except Exception as e:
        logger.error(f"Translate error: {e}")
        return ""
