"""铸想 ForgeForm v3 — 对话式 AI 工作流
对话确认需求 → 生图 → 对话确认/修改 → 建模（自动新建对话组）
"""
import sys, json, time, re
import os
from pathlib import Path
import threading
import atexit

import gradio as gr

sys.path.insert(0, str(Path(__file__).parent))
from backend.env import ROOT, COMFYUI_DIR, ensure_dirs, ensure_config

# 首次运行：config.json 不存在就从 config.example.json 复制一份，并建好输出目录。
# 必须在 import 其它 backend 模块之前 —— describer / conversation 在模块级就会读 config.json。
ensure_config()
ensure_dirs()

from backend.conversation import ConversationManager, agent_enabled  # noqa: E402
from backend import agent  # noqa: E402
from backend import bambu_print  # noqa: E402
from backend import monitor  # noqa: E402
from backend.startup import StartupChecker  # noqa: E402
from backend import describer  # noqa: E402
from backend import project_manager as pm  # noqa: E402  # 参考图上传要用（save_selected_image / save_project）
from backend import intent_classifier as ic  # noqa: E402

conv = ConversationManager()


# ─── 启动时后台拉起 ComfyUI/Ollama（独立线程，不阻塞 app）───
def _gpu_busy_lock() -> str:
    """有训练在跑就别抢显存（20GB 卡装不下 14B 训练 + 12GB 常驻对话模型）。

    train_v8.py 起训时写 finetune/TRAINING.lock，结束时删。存在就跳过预热 ——
    否则任何脚本 import app.py 都会把对话模型拉回显存，把训练拖慢 3 倍以上（实测）。
    """
    lock = Path(__file__).parent / "finetune" / "TRAINING.lock"
    return str(lock) if lock.exists() else ""


def _background_bootstrap():
    """后台自检（不再预热对话/生图模型）。

    2026-10-22 宇凯要求：铸想界面**常驻在线**，但**不加载模型**；
    有人提交问题 / 点生成时才加载所需模型。
    所以这里只探活/拉起服务进程，绝不 warm_up 模型；
    首次对话的 warm_up 仍由 on_submit 里的 agent.is_ready() 分支完成。
    """
    from backend import agent as _agent
    lock = _gpu_busy_lock()
    checker = StartupChecker()

    # 只拉起微调对话服务进程（不加载权重）；模型在首条消息时懒加载。
    if not lock and _agent._provider() == "tuned":
        try:
            checker.ensure_tuned_service()
            print("[bootstrap] 微调对话服务已就绪（模型未加载，等首条消息）")
        except Exception as e:
            print(f"[bootstrap] 微调对话服务探活异常: {e!r}")

    # ── 再做重量级自检（ComfyUI/Ollama 拉起可能要 60s+）──
    try:
        checker.check_all()  # 会检测 ComfyUI 离线→自动启动，Ollama 离线→自动启动
        # 把非 ok 的结果打出来：自动拉起失败时用户/我都能在日志里看到原因，
        # 而不是只感觉到"agent 变笨了"（其实是服务没起来、静默回退了关键词路径）
        checker.log_summary()
        print("[bootstrap] 后台自检完成（模型均未预热）")

    except Exception as e:
        print(f"[bootstrap] 自检异常: {e}")

    # ── 显存看门狗：闲置 N 分钟后把 ComfyUI 占的显存还回去 ──
    #
    # 刻意放在上面那个 try **外面**：自检失败是它自己的事，
    # 不该连带把看门狗一起关掉 —— 否则"ComfyUI 起不来"这种
    # 恰恰需要看门狗兜底的场景，反而没有看门狗。
    # daemon 线程，随主进程退出，不需要显式关闭。
    try:
        from backend import idle_watchdog
        _cfg = json.loads(
            (Path(__file__).parent / "config.json").read_text(encoding="utf-8")
        )
        idle_watchdog.start(_cfg)
    except Exception as e:
        print(f"[bootstrap] 显存看门狗启动失败（不影响使用）: {e!r}")

    if lock:
        print(f"[bootstrap] 检测到训练进行中（{lock}），跳过预热，把显存留给训练")
    print("[bootstrap] 模型按需加载：首条消息 / 首次生成时再 warm_up")


_bootstrap_thread = threading.Thread(target=_background_bootstrap, daemon=True)
_bootstrap_thread.start()


# ─── 关铸梦时自动卸载对话大模型（释放内存/显存）───
def _shutdown_cleanup():
    """进程退出时卸载 Ollama 对话模型，释放内存"""
    try:
        describer.unload_ollama()
        print("[shutdown] 已卸载对话大模型，释放内存")
    except Exception:
        pass


# 只在真正启动本应用时注册：避免其它进程 import app 后退出就误卸载共享的 Ollama 模型
if __name__ == "__main__":
    atexit.register(_shutdown_cleanup)


# ══════════════════════════════════════════════════════════════════
# 硬件监控
# ══════════════════════════════════════════════════════════════════
def svg_ring(pct, label, sub, color, size=80):
    r = 40
    c = 2 * 3.14159 * r
    off = c * (1 - min(pct, 100) / 100)
    return (
        f'<div class="ring-card">'
        f'<svg viewBox="0 0 120 120" width="{size}" height="{size}">'
        f'<circle cx="60" cy="60" r="{r}" fill="none" stroke="rgba(255,255,255,0.06)" stroke-width="7"/>'
        f'<circle cx="60" cy="60" r="{r}" fill="none" stroke="{color}" stroke-width="7"'
        f' stroke-dasharray="{c:.1f}" stroke-dashoffset="{off:.1f}" stroke-linecap="round"'
        f' transform="rotate(-90 60 60)"/>'
        f'<text x="60" y="55" text-anchor="middle" fill="#e2e2e8" font-size="20" font-weight="700"'
        f' font-family="system-ui,sans-serif">{pct}%</text>'
        f'<text x="60" y="72" text-anchor="middle" fill="#888" font-size="9"'
        f' font-family="system-ui,sans-serif">{sub}</text>'
        f'</svg>'
        f'<div style="margin-top:4px;color:{color};font-weight:600;font-size:12px">{label}</div>'
        f'</div>'
    )


def monitor_html() -> str:
    try:
        devs = monitor.get_gpu_devices()
        gpu = devs[0] if devs else {}
        vram_pct = int(100 * gpu.get("vram_used", 0) / max(gpu.get("vram_total", 1), 1))
        vram_str = f'{gpu.get("vram_used",0)//1024}G / {gpu.get("vram_total",0)//1024}G'
    except Exception:
        vram_pct = 0
        vram_str = "N/A"
    return svg_ring(vram_pct, "VRAM", vram_str, "#a78bfa")


# ══════════════════════════════════════════════════════════════════
# 侧边栏
# ══════════════════════════════════════════════════════════════════
def build_sidebar(state: dict) -> str:
    phase = state.get("phase", "empty")
    folder = state.get("folder", "")
    folder_short = folder[:18] + "…" if len(folder) > 18 else folder

    PHASE_CN = {
        "empty": "待描述", "confirm": "需求确认", "refining": "细化要求",
        "generating": "生成图片中", "selecting": "已出图", "editing": "修改图片",
        "3d": "3D 建模中", "stl": "已导出", "done": "已完成", "thinking": "思考中",
    }
    phase_txt = PHASE_CN.get(phase, phase)
    phase_done = phase in ("stl", "done")

    steps = [("confirm", "🎨", "图片"), ("selecting", "🖼", "选图"),
             ("3d", "🧊", "3D"), ("stl", "📦", "STL")]
    order = ["empty", "confirm", "generating", "selecting", "editing", "3d", "stl", "done"]
    cur = order.index(phase) if phase in order else 0

    pipe_html = '<div class="pipe-row">'
    step_phases = ["confirm", "selecting", "3d", "stl"]
    for key, icon, name in steps:
        idx = step_phases.index(key)
        active = key == phase or (phase == "generating" and key == "confirm") \
                 or (phase == "editing" and key == "selecting") or (phase == "refining" and key == "confirm")
        cls = "pipe-step done" if (idx < cur - 1 and cur > 1) else ("pipe-step active" if active else "pipe-step")
        pipe_html += f'<div class="{cls}">{icon}<br><small>{name}</small></div>'
        if idx < 3:
            pipe_html += '<div class="pipe-arrow">→</div>'
    pipe_html += '</div>'

    params = state.get("auto_params", {}) or {}
    params_html = ""
    if params:
        size = params.get("size_mm")
        size_txt = f"{size:g}mm" if isinstance(size, (int, float)) else "—"
        params_html = (
            '<div class="sb-sec"><div class="sb-label">当前参数</div>'
            f'<div class="kv"><span class="k">风格</span><span class="v">{params.get("style", "—")}</span></div>'
            f'<div class="kv"><span class="k">尺寸</span><span class="v">{size_txt}</span></div>'
            f'<div class="kv"><span class="k">颜色</span><span class="v">{params.get("color", "—")}</span></div>'
            f'<div class="kv"><span class="k">生图模型</span><span class="v">{params.get("model", "—")}</span></div>'
            '</div>'
        )

    glb = state.get("glb_path", "")
    glb_html = ""
    if glb and Path(glb).exists():
        glb_html = '<div class="sb-sec"><div class="sb-label">3D 模型</div><div class="ok-badge">✓ 已就绪</div></div>'

    stl = state.get("stl_path", "")
    stl_html = ""
    if stl and Path(stl).exists():
        stl_html = (f'<div class="sb-sec"><div class="sb-label">STL</div>'
                    f'<div class="ok-badge">✓ {state.get("stl_size",120)}mm 已导出</div></div>')

    folder_html = (f'<div class="sb-sec"><div class="sb-label">项目文件夹</div>'
                   f'<div class="folder-name">{folder_short}</div></div>')

    badge_cls = "phase-badge done" if phase_done else "phase-badge"
    return f"""
<div class="sidebar">
    <div class="sb-head">
        <div class="sb-title">当前项目</div>
        <div class="{badge_cls}">{phase_txt}</div>
    </div>
    <div class="sb-body">
        {params_html}
        {glb_html}
        {stl_html}
        <div class="sb-sec">
            <div class="sb-label">链路进度</div>
            {pipe_html}
        </div>
        {folder_html}
    </div>
</div>
"""


# ══════════════════════════════════════════════════════════════════
# CSS
# ══════════════════════════════════════════════════════════════════
CSS = """
/* ══════════════════════════════════════════════════════════════════
   铸想 ForgeForm · UI
   参考开源实现的主流做法：
     · 布局/间距    —— Open WebUI（左栏会话 + 右主区）
     · 配色/圆角    —— shadcn/ui（slate 中性色 + 单一品牌色 + 细边框）
     · 对话/输入    —— ChatGPT（消息块 + 底部大圆角输入条）
   ══════════════════════════════════════════════════════════════════ */
:root {
    --bg:        #04060d;
    /* 配色对齐 ZYKT studio（深空 + 北斗/星图）：ink 底色 + aurora 青 + nova 紫 */
    --panel:     #070b16;
    --card:      #0b1120;
    --card-hi:   #101a2e;
    --line:      rgba(150,185,235,0.14);
    --line-hi:   rgba(150,185,235,0.28);
    --txt:       #e6ecf9;
    --dim:       #8b9ac0;
    --dimmer:    #55648a;
    --brand:     #22c8f5;
    --brand-hi:  #56e0ff;
    --brand-soft:rgba(86,224,255,0.12);
    --ok:        #6ee7b7;
    --ok-soft:   rgba(110,231,183,0.13);
    --warn:      #f5b544;
    --r-lg: 14px; --r-md: 10px; --r-sm: 7px;
    --shadow: inset 0 1px 0 rgba(255,255,255,.06), 0 1px 0 rgba(2,4,9,.5), 0 20px 44px -34px rgba(0,0,0,.95);
    /* ZYKT 签名：青→紫→粉流光 + 面板顶部高光 */
    --aurora: #56e0ff;
    --nova:   #9d8dff;
    --pink:   #f472b6;
    --grad:   linear-gradient(100deg, #56e0ff 0%, #9d8dff 52%, #f472b6 100%);
    --grad-soft: linear-gradient(100deg, rgba(86,224,255,.16), rgba(157,141,255,.16));
    --panel-topline: rgba(140,234,255,0.38);
}
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }

body, .gradio-container {
    background: var(--bg) !important;
    background-image:
        radial-gradient(900px 520px at 12% -8%, rgba(86,224,255,.055), transparent 62%),
        radial-gradient(760px 480px at 88% 0%, rgba(157,141,255,.06), transparent 64%) !important;
    background-attachment: fixed !important;
    color: var(--txt) !important;
    font-family: "Inter", -apple-system, BlinkMacSystemFont, "Segoe UI",
                 "Noto Sans SC", "Microsoft YaHei", system-ui, sans-serif !important;
    font-size: 14px !important;
    -webkit-font-smoothing: antialiased;
}
.gradio-container {
    max-width: 100% !important; width: 100% !important;
    padding: 0 !important; margin: 0 !important;
}
.contain { gap: 0 !important; max-width: 100% !important; }
footer, .built-with, .show-api { display: none !important; }

/* ── 细滚动条 ─────────────────────────────────────── */
*::-webkit-scrollbar { width: 9px; height: 9px; }
*::-webkit-scrollbar-track { background: transparent; }
*::-webkit-scrollbar-thumb {
    background: rgba(255,255,255,.11); border-radius: 99px;
    border: 2px solid transparent; background-clip: content-box;
}
*::-webkit-scrollbar-thumb:hover { background: rgba(255,255,255,.2); background-clip: content-box; }

/* ── 顶部导航条 ───────────────────────────────────── */
.navbar {
    display: flex !important; align-items: center; gap: 14px;
    height: 58px; padding: 0 20px;
    background: rgba(18,21,28,.82);
    backdrop-filter: saturate(180%) blur(14px);
    border-bottom: 1px solid var(--line);
    margin: 0 !important; position: sticky; top: 0; z-index: 50;
}
.nav-logo {
    display: flex; align-items: center; gap: 9px;
    font-size: 15px; font-weight: 650; color: var(--txt); white-space: nowrap;
    letter-spacing: .2px;
}
.nav-logo .dot {
    width: 9px; height: 9px; border-radius: 99px;
    background: var(--brand); box-shadow: 0 0 12px var(--brand);
    display: inline-block;
}
/* ── 主体两栏 ─────────────────────────────────────── */
.main-wrap { gap: 0 !important; align-items: stretch !important; }
.chat-col   { padding: 0 !important; }
.side-col   { border-left: 1px solid var(--line); background: var(--panel); padding: 0 !important; }

/* ── 对话区 ───────────────────────────────────────── */
.chat-wrap { padding: 0 !important; gap: 0 !important; }
#chatbox {
    background: transparent !important; border: none !important;
    padding: 8px 0 !important;
}
/* 3D 查看器（建模完成后展示 GLB） */
#glb_viewer {
    border: 1px solid var(--line) !important;
    border-radius: var(--r-lg) !important;
    background: var(--card) !important;
    margin: 10px 0 4px 0 !important;
    overflow: hidden;
}
#glb_viewer canvas { border-radius: var(--r-lg) !important; }
#chatbox .message-row { gap: 10px; }
#chatbox .message {
    border-radius: var(--r-lg) !important;
    padding: 12px 15px !important;
    font-size: 14px !important; line-height: 1.65 !important;
    border: 1px solid transparent !important;
    box-shadow: none !important;
}
#chatbox .message.user {
    background: var(--brand-soft) !important;
    border-color: rgba(124,92,255,.22) !important;
    color: var(--txt) !important;
}
#chatbox .message.bot {
    background: var(--card) !important;
    border-color: var(--line) !important;
    color: var(--txt) !important;
}
#chatbox .message.bot p { margin: .35em 0; }
#chatbox img { border-radius: var(--r-md) !important; margin-top: 8px; }

/* ── 底部输入条（ChatGPT 风格）────────────────────── */
/* 2026-09-25 精简：两行小而清晰 ── 输入区 */
.input-row {
    align-items: center !important; gap: 8px;
    padding: 6px 18px !important;
    background: linear-gradient(180deg, transparent, var(--bg) 35%);
}
.input-row-chat { padding-bottom: 4px !important; }
.input-row-upload { padding-bottom: 12px !important; padding-top: 0 !important; }
#chat_input textarea, #chat_input input {
    border-radius: var(--r-md) !important;
    background: var(--card) !important;
    border: 1px solid var(--line-hi) !important;
    padding: 8px 12px !important;
    font-size: 13.5px !important;
    color: var(--txt) !important;
    box-shadow: var(--shadow);
    transition: border-color .15s, box-shadow .15s;
    resize: none;
    min-height: 36px !important;
}
#chat_input textarea::placeholder { color: var(--dimmer) !important; }
#chat_input textarea:focus, #chat_input input:focus {
    border-color: var(--brand) !important;
    box-shadow: 0 0 0 3px var(--brand-soft) !important;
}
#send_btn {
    border-radius: var(--r-md) !important;
    height: 36px; min-width: 72px;
    background: var(--brand) !important; border: none !important;
    color: #fff !important; font-weight: 600 !important; font-size: 13px !important;
    box-shadow: 0 2px 12px -4px var(--brand);
    transition: background .15s, transform .1s;
}
#send_btn:hover { background: var(--brand-hi) !important; }
#send_btn:active { transform: translateY(1px); }

/* ── 上传 / 按提示词改图行（修复暗底黑字、按钮被挤扁）── */
:root {
    --button-secondary-text-color: #e6ecf7 !important;
    --button-secondary-background-fill: #141b2a !important;
    --button-secondary-border-color: #2b3852 !important;
}
.input-row-upload {
    align-items: stretch !important;
    gap: 8px !important;
    padding: 2px 18px 14px !important;
}
#ref_up, #ref_up button {
    color: #0b1020 !important;
    background: #e6ecf7 !important;
    border: 1px solid #e6ecf7 !important;
    border-radius: var(--r-md) !important;
    height: 36px !important;
    min-width: 108px !important;
    font-size: 12.5px !important;
    box-shadow: var(--shadow);
    white-space: nowrap;
}
#ref_up:hover, #ref_up button:hover { background: #ffffff !important; border-color: #ffffff !important; }
#ref_up * { color: #0b1020 !important; }
#ref_up input[type="file"] { color: var(--txt) !important; }
#edit_prompt {
    flex: 1 1 260px !important;
    min-width: 0 !important;
}
#edit_prompt textarea, #edit_prompt input {
    background: var(--card) !important;
    border: 1px solid var(--line-hi) !important;
    border-radius: var(--r-md) !important;
    color: var(--txt) !important;
    font-size: 13px !important;
    min-height: 36px !important;
    padding: 8px 12px !important;
    box-shadow: var(--shadow);
}
#edit_prompt textarea::placeholder { color: var(--dimmer) !important; }
#edit_prompt textarea:focus { border-color: var(--brand) !important;
    box-shadow: 0 0 0 3px var(--brand-soft) !important; }
#btn_edit_submit {
    height: 36px !important;
    min-width: 118px !important;
    white-space: nowrap;
    border-radius: var(--r-md) !important;
    background: var(--card) !important;
    color: var(--txt) !important;
    border: 1px solid var(--line-hi) !important;
    font-size: 12.5px !important;
}
#btn_edit_submit:hover { border-color: var(--brand) !important; color: var(--brand-hi) !important; }
@media (max-width: 720px) {
    .input-row-upload { flex-wrap: wrap !important; }
    #edit_prompt { flex: 1 1 100% !important; order: -1; }
    #btn_edit_submit { flex: 1 1 auto !important; }
}

/* ── 动作按钮（按阶段出现）────────────────────────── */
.action-bar {
    display: flex; gap: 9px; padding: 4px 22px 2px;
    flex-wrap: wrap; align-items: center;
}
.action-bar button {
    border-radius: 99px !important;
    height: 38px !important; padding: 0 18px !important;
    font-size: 13px !important; font-weight: 550 !important;
    border: 1px solid var(--line-hi) !important;
    background: var(--card) !important; color: var(--txt) !important;
    transition: background .15s, border-color .15s, transform .1s;
    box-shadow: none !important;
}
.action-bar button:hover { background: var(--card-hi) !important; border-color: var(--line-hi) !important; }
.action-bar button:active { transform: translateY(1px); }
.action-bar button.primary {
    background: var(--brand) !important; border-color: var(--brand) !important;
    color: #fff !important; box-shadow: 0 2px 12px -5px var(--brand);
}
.action-bar button.primary:hover { background: var(--brand-hi) !important; }

/* ── 侧边栏 ───────────────────────────────────────── */
.sidebar { padding: 18px 16px 26px; border: none; background: transparent; margin: 0; }
.sb-head {
    display: flex; align-items: center; justify-content: space-between;
    padding-bottom: 12px; margin-bottom: 4px; border-bottom: 1px solid var(--line);
}
.sb-title { font-size: 13.5px; font-weight: 650; color: var(--txt); letter-spacing: .2px; }
.sb-body { display: flex; flex-direction: column; gap: 2px; margin-top: 6px; }
.sb-sec { padding: 13px 0; border-bottom: 1px solid var(--line); }
.sb-sec:last-child { border-bottom: none; }
.sb-label {
    font-size: 10.5px; font-weight: 600; letter-spacing: .09em; text-transform: uppercase;
    color: var(--dimmer); margin-bottom: 9px;
}
.phase-badge {
    display: inline-flex; align-items: center; gap: 6px;
    padding: 4px 11px; border-radius: 99px;
    font-size: 11.5px; font-weight: 600;
    background: var(--brand-soft); color: var(--brand-hi);
    border: 1px solid rgba(124,92,255,.22);
}
.phase-badge.done { background: var(--ok-soft); color: var(--ok); border-color: rgba(61,220,151,.26); }
.kv { display: flex; justify-content: space-between; gap: 10px; font-size: 12.5px; padding: 3px 0; }
.kv .k { color: var(--dimmer); }
.kv .v { color: var(--txt); text-align: right; }
.folder-name {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 11px; color: var(--dimmer); word-break: break-all; line-height: 1.5;
}
.ok-badge { color: var(--ok); font-size: 12.5px; padding: 3px 0; display: flex; gap: 7px; align-items: center; }

/* 链路进度：时间轴药丸 */
.pipe-row { display: flex; align-items: center; gap: 5px; flex-wrap: wrap; }
.pipe-step {
    text-align: center; padding: 7px 10px; border-radius: 99px;
    font-size: 11px; background: var(--card); min-width: 54px;
    border: 1px solid var(--line); color: var(--dim);
    transition: all .2s;
}
.pipe-step.done  { background: var(--ok-soft); border-color: rgba(61,220,151,.3); color: var(--ok); }
.pipe-step.active{ background: var(--brand-soft); border-color: rgba(124,92,255,.4);
                   color: var(--brand-hi); box-shadow: 0 0 0 3px rgba(124,92,255,.08); }
.pipe-step small { display: block; font-size: 10px; margin-top: 1px; opacity: .85; }
.pipe-arrow { color: var(--dimmer); font-size: 11px; }

/* ── 硬件环形卡 ───────────────────────────────────── */
.ring-card { display: inline-block; text-align: center; padding: 2px 6px; }

/* ── 空状态（欢迎页）──────────────────────────────── */
.empty-hero { padding: 46px 26px 26px; text-align: center; }
.empty-hero h2 {
    font-size: 25px; font-weight: 680; margin: 0 0 8px;
    letter-spacing: -.02em; color: var(--txt);
}
.empty-hero h2 .accent { color: var(--brand-hi); }
.empty-hero p { color: var(--dim); font-size: 13.5px; margin: 0 0 22px; }
.chips { display: flex; gap: 9px; justify-content: center; flex-wrap: wrap; }
.chip {
    font-size: 12.5px; color: var(--dim); padding: 9px 15px;
    border: 1px solid var(--line); border-radius: 99px; background: var(--card);
    transition: all .15s;
}
.chip:hover { border-color: var(--brand); color: var(--txt); transform: translateY(-1px); }

/* ── 通用 Gradio 收敛 ─────────────────────────────── */
.block, .form { background: transparent !important; border: none !important; }
label > span, .label-wrap span { color: var(--dim) !important; font-size: 12px !important; }

/* ═══ ZYKT studio 风格：流光 / 面板高光 / 渐变按钮 ═══ */

/* 面板的顶部高光线（ZYKT 每张卡顶部那一道细光） */
.gradio-container .block,
.gradio-container .form,
.gradio-container .panel {
    position: relative;
    border-radius: var(--r-lg) !important;
    border-color: var(--line) !important;
    background: linear-gradient(180deg, rgba(16,25,43,.94), rgba(8,13,24,.96)) !important;
    box-shadow: var(--shadow) !important;
}
.gradio-container .block::before {
    content: "";
    position: absolute; inset: 0 12% auto 12%; height: 1px;
    background: linear-gradient(90deg, transparent, var(--panel-topline), transparent);
    pointer-events: none;
}

/* 主按钮：青→紫渐变 */
#send_btn, #btn_gen, #btn_model, #btn_stl,
button.lg.primary, button.primary {
    background: var(--grad) !important;
    border: 0 !important;
    color: #04060d !important;
    font-weight: 650 !important;
    letter-spacing: .01em;
    box-shadow: 0 10px 26px -16px rgba(86,224,255,.85) !important;
    transition: filter .18s ease, transform .12s ease;
}
#send_btn:hover, #btn_gen:hover, #btn_model:hover, #btn_stl:hover,
button.primary:hover { filter: brightness(1.07) saturate(1.05); }
#send_btn:active, button.primary:active { transform: translateY(1px); }

/* 次按钮：ghost + 青色描边悬停 */
button.secondary {
    background: var(--ghost, rgba(19,29,49,.9)) !important;
    border: 1px solid var(--line-hi) !important;
    color: var(--txt) !important;
}
button.secondary:hover {
    border-color: var(--brand-hi) !important;
    color: var(--brand-hi) !important;
    box-shadow: 0 0 0 1px rgba(86,224,255,.16), 0 10px 26px -22px rgba(86,224,255,.8);
}

/* 示例胶囊：细边框 + 悬停泛青 */
.gradio-container .gr-button-secondary,
.gradio-container .examples button {
    background: linear-gradient(180deg, rgba(20,31,53,.96), rgba(10,16,30,.96)) !important;
    border: 1px solid var(--line) !important;
    border-radius: 999px !important;
}

/* 输入框：深一档的底 + 聚焦泛青 */
textarea, input[type="text"], input[type="search"] {
    background: rgba(5,8,15,.92) !important;
    border: 1px solid var(--line) !important;
    color: var(--txt) !important;
    border-radius: var(--r-md) !important;
}
textarea:focus, input[type="text"]:focus {
    border-color: rgba(86,224,255,.55) !important;
    box-shadow: 0 0 0 3px rgba(86,224,255,.10) !important;
    outline: none !important;
}

/* 标题流光（ZYKT 字标那套青→蓝→紫的扫光） */
.gradio-container h1,
.gradio-container .md h1 {
    background: var(--grad);
    -webkit-background-clip: text; background-clip: text;
    color: transparent !important;
    font-weight: 750 !important;
    letter-spacing: -.01em;
}

/* 对话气泡：用户侧偏青、助手侧用卡片底 */
.gradio-container .message.user,
.gradio-container .bubble-wrap.user {
    background: linear-gradient(135deg, rgba(34,200,245,.16), rgba(122,102,245,.16)) !important;
    border: 1px solid rgba(86,224,255,.22) !important;
}

/* 滚动条也跟上（ZYKT 的 thumb 是 #1b2740） */
*::-webkit-scrollbar-thumb { background: #1b2740; }
*::-webkit-scrollbar-thumb:hover { background: #26385c; }



/* ═══ APP 化布局：单列、顶部栏吸附、输入条固定底部 ═══
   目标是把浏览器里的宽表格布局收成手机 App 的形状：
   一个 480px 的内容列居中，头/输入条吸附，侧栏收成抽屉。
   只动布局和尺寸，不动任何组件的显隐逻辑（_buttons_for_phase 仍然说了算）。 */

html, body { height: 100%; }
body, .gradio-container { min-height: 100vh; }
.gradio-container {
    max-width: 520px !important;
    margin: 0 auto !important;
    padding: 0 !important;
    min-height: 100vh;
    position: relative;
    box-shadow: 0 0 0 1px var(--line), 0 40px 120px -60px rgba(0,0,0,.95);
}
@media (max-width: 560px) {
    .gradio-container { max-width: 100% !important; box-shadow: none; }
}

/* 顶部栏 */
.navbar {
    position: sticky !important; top: 0; z-index: 60;
    margin: 0 !important; padding: 8px 12px !important;
    border-bottom: 1px solid var(--line) !important;
    background: rgba(7,11,22,.86) !important;
    backdrop-filter: blur(14px);
    align-items: center !important;
}
.navbar .nav-logo { font-size: 14px !important; }

/* 主区：改成单列 */
.main-wrap { flex-direction: column !important; gap: 0 !important; }
.chat-col { min-width: 0 !important; width: 100% !important; max-width: 100% !important; gap: 0 !important; }
.side-col {
    display: none !important;      /* 窄屏先收起来，防止把主区挤窄 */
}

/* 对话区占满、可滚 */
.chat-col > .block:first-of-type,
#chatbox {
    flex: 1 1 auto !important;
    min-height: 0 !important;
    border: 0 !important;
    background: transparent !important;
    box-shadow: none !important;
    border-radius: 0 !important;
    padding: 6px 12px !important;
}
.chat-col { display: flex; flex-direction: column; height: calc(100vh - 56px); }

/* 输入条固定底部 */
.input-row-chat {
    position: sticky !important; bottom: 0; z-index: 55;
    padding: 10px 12px calc(10px + env(safe-area-inset-bottom)) !important;
    margin: 0 !important;
    background: rgba(7,11,22,.9) !important;
    backdrop-filter: blur(14px);
    border-top: 1px solid var(--line) !important;
    gap: 8px !important;
}
.input-row-chat textarea {
    border-radius: 999px !important;
    padding: 11px 16px !important;
    min-height: 44px !important;
    resize: none !important;
}
#send_btn {
    border-radius: 999px !important;
    min-width: 44px !important; width: 44px;
    height: 44px !important; min-height: 44px !important;
    font-size: 0 !important;             /* 藏掉文字，只留图形 */
    position: relative;
}
#send_btn::after {
    content: "↑"; font-size: 18px; font-weight: 700;
    position: absolute; inset: 0; display: grid; place-items: center;
}

/* 动作条（生成图片 / 建模 / 导出 STL）做成胶囊 */
.action-bar {
    padding: 0 12px 8px !important;
    margin: 0 !important;
    gap: 8px !important;
    flex-wrap: nowrap !important;
    overflow-x: auto;
}
.action-bar button {
    border-radius: 999px !important;
    padding: 9px 14px !important;
    font-size: 12.5px !important;
    white-space: nowrap;
}

/* 上传/改写那一行压低存在感 */
.input-row-upload {
    padding: 0 12px 6px !important;
    margin: 0 !important;
    gap: 8px !important;
    opacity: .92;
}
.input-row-upload button {
    border-radius: 999px !important; font-size: 12px !important; padding: 7px 12px !important;
}

/* 对话气泡圆角化 */
#chatbox .message, #chatbox .bubble {
    border-radius: 18px !important;
    border: 1px solid var(--line) !important;
    background: linear-gradient(180deg, rgba(16,25,43,.95), rgba(8,13,24,.96)) !important;
}
#chatbox .user, #chatbox .message.user {
    border-color: rgba(86,224,255,.28) !important;
    background: linear-gradient(135deg, rgba(34,200,245,.16), rgba(122,102,245,.16)) !important;
}



/* ── 修正：视口高度、侧栏彻底隐藏、顶部栏不截断 ── */
body {
    background-attachment: fixed;
    min-height: 100dvh;
}
.gradio-container {
    display: flex; flex-direction: column;
    height: 100dvh; min-height: 100dvh;
    overflow: hidden;
}

/* 顶部栏里的下拉要能被压缩，不然品牌名被挤掉 */
.navbar > * { min-width: 0 !important; }
.navbar .nav-logo { flex: 0 0 auto !important; white-space: nowrap; }
.navbar #history_dd, .navbar .block:has(#history_dd) { flex: 1 1 auto !important; min-width: 0 !important; }
.navbar #history_dd input, .navbar select { font-size: 12px !important; }

/* 主区撑满剩余高度 */
.main-wrap {
    flex: 1 1 auto !important;
    min-height: 0 !important;
    height: auto !important;
    overflow: hidden !important;
}
.gradio-container .side-col { display: none !important; }

.chat-col {
    display: flex !important;
    flex-direction: column !important;
    height: 100% !important;
    min-height: 0 !important;
    overflow: hidden !important;
}
#chatbox {
    flex: 1 1 auto !important;
    min-height: 0 !important;
    overflow-y: auto !important;
}
.input-row-chat, .input-row-upload, .action-bar {
    flex: 0 0 auto !important;
    position: static !important;
}



/* ── 最终布局：底部输入用 fixed 钉死，主区留出等高的底部内边距 ──
   为什么不用 sticky：Gradio 的 .contain / .wrap / .main 是三层 flex，
   sticky 会被中间那层的 overflow 重置，表现是"输入条跑到屏幕外"。 */

.input-row-chat {
    position: fixed !important;
    left: 50% !important; transform: translateX(-50%) !important;
    bottom: 0 !important;
    width: min(520px, 100vw) !important;
    max-width: 520px !important;
    margin: 0 !important;
    padding: 10px 14px calc(12px + env(safe-area-inset-bottom)) !important;
    background: rgba(7,11,22,.94) !important;
    backdrop-filter: blur(16px);
    border-top: 1px solid var(--line) !important;
    z-index: 80 !important;
    gap: 10px !important;
}
.input-row-upload {
    position: fixed !important;
    left: 50% !important; transform: translateX(-50%) !important;
    bottom: calc(66px + env(safe-area-inset-bottom)) !important;
    width: min(520px, 100vw) !important;
    max-width: 520px !important;
    margin: 0 !important;
    padding: 6px 14px !important;
    background: rgba(7,11,22,.9) !important;
    backdrop-filter: blur(16px);
    z-index: 79 !important;
    gap: 8px !important;
}
.action-bar {
    position: fixed !important;
    left: 50% !important; transform: translateX(-50%) !important;
    bottom: calc(108px + env(safe-area-inset-bottom)) !important;
    width: min(520px, 100vw) !important;
    max-width: 520px !important;
    margin: 0 !important;
    padding: 6px 14px !important;
    background: linear-gradient(180deg, rgba(7,11,22,0) 0%, rgba(7,11,22,.9) 30%) !important;
    z-index: 78 !important;
}

/* 给固定的三条让出高度，否则最后一条消息被盖住 */
.chat-col { padding-bottom: 168px !important; }
#chatbox { padding-bottom: 8px !important; }

/* 顶部栏固定在顶，内容从它下面开始 */
.navbar {
    position: fixed !important; top: 0 !important; left: 50% !important;
    transform: translateX(-50%) !important;
    width: min(520px, 100vw) !important; max-width: 520px !important;
    z-index: 90 !important;
}
.chat-col { padding-top: 58px !important; }

/* 品牌名在窄屏只留两个字 */
/* 品牌名已直接改成「铸想」，不再依赖 CSS 伪元素（伪元素在 flex 里不稳定） */

"""


# Gradio 主题（字体/圆角/品牌色统一交给主题，CSS 只做细调）
THEME = None
try:
    THEME = gr.themes.Soft(
        primary_hue=gr.themes.colors.violet,
        neutral_hue=gr.themes.colors.slate,
        spacing_size=gr.themes.sizes.spacing_md,
        radius_size=gr.themes.sizes.radius_md,
        font=["Inter", "-apple-system", "BlinkMacSystemFont", "Segoe UI",
              "Noto Sans SC", "Microsoft YaHei", "system-ui", "sans-serif"],
        font_mono=["ui-monospace", "SFMono-Regular", "Menlo", "monospace"],
    ).set(
        body_background_fill="#0e1116",
        body_text_color="#e7eaf0",
        block_background_fill="#171b24",
        block_border_color="rgba(255,255,255,0.07)",
        block_label_text_color="#98a1b0",
        input_background_fill="#171b24",
        input_border_color="rgba(255,255,255,0.13)",
        button_primary_background_fill="#7c5cff",
        button_primary_background_fill_hover="#9179ff",
        button_primary_text_color="#ffffff",
        button_secondary_background_fill="#171b24",
        button_secondary_text_color="#e7eaf0",
        border_color_primary="rgba(255,255,255,0.07)",
    )
except Exception as _e:  # 主题 API 变动时不影响启动
    print(f"[DSH] 主题构建跳过: {_e}")


# ══════════════════════════════════════════════════════════════════
# UI 事件
# ══════════════════════════════════════════════════════════════════
def _chat_value(conv_list) -> list:
    """把对话记录转成 Chatbot 的 (user, assistant) 列表。

    Gradio tuples 格式里，嵌套 tuple 必须是 (文件路径, 说明文字)——
    postprocess 会把 tuple[0] 当作文件路径、tuple[1] 当作 alt 文本。
    （写反会报 OSError: Invalid argument，因为 Gradio 拿文字当路径去 open。）
    """
    out = []
    for e in conv_list:
        role = e.get("role", "user")
        content = e.get("content", "")
        ai_reply = e.get("ai_reply", "")
        images = e.get("images", [])
        if role == "user" and content:
            out.append([content, None])
        if ai_reply:
            # 带图：tuple = (图片路径, 文字说明)。
            # 注意 Gradio 5.50 的 tuples 格式**一条消息只支持一个文件**
            # （多文件嵌套元组会被当成 (path, alt_text) 解析并报 ValidationError），
            # 每张图分别占一条消息。
            files = [p for p in images if p]
            bot_msg = (files[0], ai_reply) if files else ai_reply
            if out and out[-1][1] is None:
                out[-1][1] = bot_msg
            else:
                out.append([None, bot_msg])
            if files:
                for file in files[1:]:
                    out.append([None, (file, '')])
                # File captions are alt text in Gradio, not a visible reply.
                out.append([None, ai_reply])
    return out


def _buttons_for_phase(phase: str, state: dict = None) -> dict:
    """根据阶段返回按钮可见性。empty 阶段无任何按钮。

    「导出 STL」按**有没有模型**决定，不是按阶段：改了尺寸想重新导、或自动那步没跑成，
    用户都得有个能点的地方（2026-09-23 用户反馈"缺少 glb 转 stl 这一步"）。
    「🎯 采用为参考图」：only selecting 阶段且 state.ref_upload_pending 时显示
    （on_reference_search 后用户必须确认 Bing 拿的图是不是想要的）。
    """
    v = lambda b: gr.update(visible=b)
    off = {"refine": v(False), "gen": v(False), "edit": v(False), "model": v(False),
           "new": v(False), "stl": v(False), "adopt": v(False)}
    has_model = bool((state or {}).get("glb_path"))
    def with_stl(d):
        d = dict(d)
        d["stl"] = v(has_model)
        d["adopt"] = v(bool((state or {}).get("ref_upload_pending")))
        return d
    if phase == "empty":
        return off
    if phase == "confirm":
        return with_stl({"refine": v(True), "gen": v(True), "edit": v(False), "model": v(False), "new": v(False)})
    if phase == "refining":
        return with_stl({"refine": v(True), "gen": v(True), "edit": v(False), "model": v(False), "new": v(False)})
    if phase in ("selecting", "editing"):
        # 生成图后：修改图片 / 生成建模；刚联网找的图还要"🎯 采用"按钮
        out = with_stl({"refine": v(False), "gen": v(False), "edit": v(True),
                        "model": v(True), "new": v(False),
                        "adopt": v(bool((state or {}).get("ref_upload_pending")))})
        return out
    if phase == "generating":
        # ★★ 2026-10-19 修「出图后按钮全消失」★★
        #
        # 现象（宇凯报的、我按官方启动入口复现）：
        #   说需求 → 出图那一步 → 界面上「🔧 修改图片」「🧊 生成建模」都不见了，
        #   只剩「发送」。用户以为核心功能没了。
        #
        # 根因：`_do_generate_images` 一进来就把 phase 设成 "generating" 并落盘。
        #   出图**失败/超时/服务重启**时，phase 永远停在 "generating"，
        #   而这里**没有 generating 的分支** → 落到函数最后 return with_stl(off)
        #   → 所有按钮隐藏 ✗（实测组 2026-10-19_131808：phase=generating、candidates=[]）
        #
        # 修法：generating 但**还没图** → 说明这一步没成，把"生成图片"还回来让用户重试；
        #   已经有图（生成过程中的二次调用）→ 按 selecting 处理（能改图、能建模）。
        if (state or {}).get("candidates"):
            return with_stl({"refine": v(False), "gen": v(False), "edit": v(True),
                             "model": v(True), "new": v(False)})
        return with_stl({"refine": v(True), "gen": v(True), "edit": v(False),
                         "model": v(False), "new": v(True)})
    if phase == "3d":
        # ★★ 2026-10-19 修「核心功能全消失」★★
        #
        # 现象（宇凯报的）：进铸想 → 说需求 → 界面**没有任何可点的按钮**，
        #   像是核心功能全没了。
        #
        # 根因：phase 停在 "3d" 时这里 return with_stl(off) —— 把**所有**按钮藏起来。
        #   而 "3d" 是"正在建模"的中间态：建模正常跑完会推到 stl，没问题；
        #   但**建模失败/被打断/服务重启**时 phase 就永远卡在 "3d"，
        #   glb_path 一直是 None → 界面永久无按钮，用户只能刷新或新建组 ✗
        #   （实测组 2026-10-19_105832：phase=3d、glb=None、按钮全空）
        #
        # 修法：3d 阶段**只有真的还在建**才藏按钮；没有模型就当"选题"阶段处理，
        #   让用户能重新点「🧊 生成建模」重试，而不是卡死在那里。
        if has_model:
            return with_stl(off)
        # 没有模型 → 说明这一步没成，给回可操作按钮（重试建模 / 改图 / 新建）
        return with_stl({"refine": v(False), "gen": v(False), "edit": v(True),
                         "model": v(True), "new": v(True)})
    if phase == "stl":
        return with_stl({"refine": v(False), "gen": v(False), "edit": v(False),
                        "model": v(False), "new": v(True)})
    return with_stl(off)


def ring_html(stage: str = "", elapsed: float = 0.0, done: bool = False) -> str:
    """环形高亮进度条（自包含 HTML/SVG + CSS 动画，**前端自己跑**）。

    为什么这么做：生图/3D 这些长步骤在 Gradio handler 里是**阻塞**的，后端推不出中间进度 ✗。
    把"实测中位"编成动画时长交给浏览器走，既有真实进度感，又不假装 100%：
    走到中位时停在 ~95%，真实完成后由后端换成勾（宁可等，也不编假进度）。
    数字全部来自 backend/timings（实测样本，文案里带上 n 与最长值）。
    """
    import html as _h
    import backend.timings as T
    if not stage:
        return "<div style='height:2px'></div>"
    med, hi = T.estimate(stage)
    lab = _h.escape(T.label(stage))
    R = 26.0
    C = 2 * 3.141592653589793 * R
    if done:
        off, anim, color, inner, dur = 0.0, "", "#1f9d55", "✓", 0.0
    else:
        # 关键帧从满圈(C)走到 5%(=95% 进度)，停在 95% 等真实完成
        off, anim, color, inner, dur = C, f"animation:ringrun {max(1.5, med):.1f}s linear forwards;", "#2563eb", "…", max(1.5, med)
    note = _h.escape(f"实测通常 {T.human(med)}（n={T.samples(stage)}）· 最长见过 {T.human(hi)}")
    if done:
        note = _h.escape(f"实际 {T.human(elapsed)} · 实测中位 {T.human(med)}（n={T.samples(stage)}）")
    return f"""<style>
@keyframes ringrun {{ from {{ stroke-dashoffset: {C:.1f} }} to {{ stroke-dashoffset: {C*0.05:.1f} }} }}
</style>
<div style="display:flex;align-items:center;gap:12px;padding:4px 2px;font-size:13px;color:#374151">
  <svg width="60" height="60" viewBox="0 0 60 60" style="flex:0 0 60px">
    <circle cx="30" cy="30" r="{R}" fill="none" stroke="#e5e7eb" stroke-width="6"/>
    <circle cx="30" cy="30" r="{R}" fill="none" stroke="{color}" stroke-width="6" stroke-linecap="round"
            transform="rotate(-90 30 30)" stroke-dasharray="{C:.1f}" stroke-dashoffset="{off:.1f}"
            style="{anim}"/>
    <text x="30" y="35" text-anchor="middle" font-size="14" fill="#111827">{inner}</text>
  </svg>
  <div>
    <div style="font-weight:600">{lab}{' · 完成' if done else ' 进行中…'}</div>
    <div style="color:#6b7280">{note}</div>
  </div>
</div>
"""


def _update_ui_with_note(state: dict, note: str, glb_override: str = None, ring: str = None):
    """在 _update_ui 的基础上，于对话框末尾追加一条临时提示（不写入 state，不落盘）"""
    vals = _update_ui(state, glb_override, ring)
    chat = list(vals[0]) + [[None, note]]
    return (chat,) + tuple(vals[1:])


def on_submit(user_input: str, state: dict):
    """提交输入（generator）。

    2026-09-22：**agent 优先** —— 让 LLM 读对话决定这一轮要做什么动作
    （改参数/出图/改图/建模/导 STL/打印/新建组/纯问答），后端校验并执行。
    agent 不可用/解析失败时才回退下面的关键词+流式路径。
    """
    if not user_input or not user_input.strip():
        yield "", state, *_update_ui(state)
        return

    # ── agent 主导（流式：边推理边显示，首字不再干等）──
    if agent_enabled():
        # 预热的是 agent **实际要用**的模型：provider=tuned 时是微调服务，
        # 不能去预热 Ollama —— 那会白占 12.9GB 把微调模型挤出显存（踩过）
        if not agent.is_ready():
            yield "", state, *_update_ui_with_note(state, "⏳ 首次加载对话模型（约 10 秒），请稍候…")
            agent.warm_up()
        yield "", state, *_update_ui_with_note(state, "🤔 正在分析你的意图…")
        res, last_emit = None, 0.0
        try:
            for ev in conv.handle_agent_turn_stream(user_input):
                if "result" in ev:
                    res = ev["result"]
                    break
                partial, tchars = (ev.get("reply") or "").strip(), ev.get("thinking_chars") or 0
                now = time.time()
                if now - last_emit < 0.25:          # 限流：别把前端刷爆
                    continue
                last_emit = now
                if partial:
                    # 正文边生成边显示（尾部 700 字，够看清在写什么）
                    yield "", state, *_update_ui_with_note(state, "✍️ " + partial[-700:])
                elif tchars:
                    yield "", state, *_update_ui_with_note(state, f"🧠 正在推理…（{tchars} 字）")
        except Exception as e:
            print(f"[DSH] ⚠️ agent 流式异常，回退：{e!r}")
            res = None
        if res:
            # 把决策过程摊开给用户看：先亮模型的真实推理结论，再亮它判断出的意图与动作
            gist = (res.get("thinking") or "").strip()
            if gist:
                yield "", res["project"], *_update_ui_with_note(
                    res["project"], f"🧠 思考：{gist[:220]}{'…' if len(gist) > 220 else ''}")
            for s in (res.get("understanding") or [])[:3]:
                yield "", res["project"], *_update_ui_with_note(res["project"], f"🎯 判断：{s}")
                time.sleep(0.2)
            act_names = [a["name"] for a in (res.get("actions") or [])]
            if act_names:
                yield "", res["project"], *_update_ui_with_note(
                    res["project"], "⚙️ 执行：" + " → ".join(act_names))
            # 把这一轮真做出来的模型交给查看器（modeling 刚完成时 res 里带 glb）
            yield "", res["project"], *_update_ui(res["project"], res.get("glb"))
            return

    phase = (state or {}).get("phase", "empty")
    intent, _ = ic.classify(user_input, phase)

    # ── 需求聊天：流式 LLM 总结（empty/confirm/refining 都可以一直聊，累积上下文）──
    if phase in ("empty", "confirm", "refining") and \
            intent in (ic.Intent.DESCRIBE_OBJECT, ic.Intent.REFINE):
        st, resolved, params = conv.prepare_describe(user_input)
        # 模型若不在内存里，先补预热（否则首字要等 10s 冷加载）
        if not describer.is_model_resident():
            yield "", st, *_update_ui_with_note(st, "⏳ 首次加载对话模型（约 10 秒），请稍候…")
            describer.warm_up_ollama()

        # ── 理解过程：把 AI 怎么解析这条需求逐条摊开给用户看 ──
        notes = params.get("notes") or [resolved]
        p = params
        size = p.get("size_mm")
        size_txt = f"{size:g}mm" if isinstance(size, (int, float)) else "未指定"
        steps = [
            f"⏳ 正在理解你的需求…",
            (f"🔍 第 {len(notes)} 轮 · 解析到：{resolved[:46]}"
             if params.get("followup") else f"🔍 识别主体：{resolved[:46]}"),
            ("📐 本轮实际改动：" + "；".join(params.get("changes") or []))
            if params.get("changes") else "📐 本轮未改动颜色/尺寸/风格",
            (f"🧠 综合 {len(notes)} 轮上下文 → 风格 {p.get('style')} ｜ 尺寸 {size_txt}"
             f" ｜ 颜色 {p.get('color')} ｜ 模型 {p.get('model')}"),
        ]
        for s in steps:
            yield "", st, *_update_ui_with_note(st, s)
            time.sleep(0.35)          # 留出可读时间，否则几条会瞬间闪过

        yield "", st, *_update_ui_with_note(st, "✍️ 正在组织总结…")
        acc = ""
        for partial in describer.generate_confirm_response_stream(user_input, resolved, params):
            acc = partial
            yield "", st, *_update_ui_with_note(st, acc)
        if not acc:
            acc = describer._fallback_confirm_response(user_input, resolved, params)
            yield "", st, *_update_ui_with_note(st, acc)
        result = conv.finish_reply(st, user_input, acc)
        yield "", result["project"], *_update_ui(result["project"])
        return

    # ── 其它阶段：普通流程（细化/生图/选图等本身秒回）──
    yield "", state, *_update_ui_with_note(state, "⏳ 正在处理…")
    result = conv.handle_user_input(user_input)
    # 若这轮触发了 3D，result 带 glb（交给查看器；模型留在本组，轮转推迟到下一条消息）
    yield "", result["project"], *_update_ui(result["project"], result.get("glb"))


def on_btn_refine(state: dict):
    """继续细化要求（2026-09-25 修：同步函数 → generator，否则 Gradio 不响应）"""
    if state is None:
        state = {}
    result = conv.handle_user_input("继续细化要求")
    state = result.get("project", state)
    yield state, *_update_ui(state)


def _default_3d_model() -> str:
    """config.json 里的默认 3D 模型（别用 conv.config —— ConversationManager 没有 self.config，
    照 backend/conversation.agent_enabled() 的读法直接读文件）。"""
    try:
        cfg = json.loads((Path(__file__).parent / "config.json").read_text(encoding="utf-8"))
        return cfg.get("stages", {}).get("3d", {}).get("default_model") or "hunyuan21"
    except Exception:
        return "hunyuan21"


def on_reference_uploaded(file_obj, state: dict):
    """用户上传一张参考图 → 直接作为本组的"已选图"，点「生成建模」就按它建模。

    为什么需要这一步（宇凯 2026-09-23："我要熊大，但图像只是给了一个卡通小熊"）：
    熊大是 IP 角色，**任何纯文字生图模型都只能猜** ✗ —— 换多大的模型都一样。
    可靠路线是"照这张图画"：让用户把参考图丢进来，直接拿它建模（或喂给图生图/编辑模型）。
    以前界面**完全没有上传入口** ✗，参考图只能靠联网搜（相关性还不稳）。
    """
    import shutil
    if not state or not state.get("path"):
        yield state, *_update_ui_with_note(state, "还没建对话组，先发一句话再上传参考图。")
        return
    src = getattr(file_obj, "name", None) or (file_obj if isinstance(file_obj, str) else None)
    if not src or not Path(src).exists():
        src = getattr(file_obj, "path", None)          # gr.File 新版把路径放 .path
    if not src or not Path(src).exists():
        yield state, *_update_ui_with_note(state, "没读到图片文件，换一张再试（png/jpg/webp）。")
        return
    try:
        proj = Path(state["path"])
        cdir = proj / "candidates"
        cdir.mkdir(parents=True, exist_ok=True)
        dst = cdir / f"ref_{int(time.time())}{Path(src).suffix.lower() or '.png'}"
        shutil.copyfile(src, dst)
        cands = list(state.get("candidates") or [])
        cands.append(str(dst))
        state["candidates"] = cands
        state["selected_idx"] = len(cands) - 1
        state["selected"] = str(dst)
        # 标记"这是用户给的参考图" → 下次点生图时走 Qwen-Image-2.1 参考图工作流
        # （照图生成指定角色；纯文字生图对 IP 角色只能猜 ✗）
        state["ref_upload"] = str(dst)
        pm.save_selected_image(proj, dst.read_bytes())      # 落盘 selected.jpg，刷新后不丢
        state["phase"] = "selecting"                        # → 按钮出「修改图片 / 🧊 生成建模」
        conv._append_conversation(
            state, "user", "（上传了一张参考图）", "", images=[str(dst)])
        conv._append_conversation(
            state, "assistant", "",
            "✅ 已收到参考图，作为本组的造型依据。\n\n"
            "下一步：点 **🧊 生成建模** 就按这张图做 3D 模型（IP 角色/特定形象这条最稳 —— "
            "纯文字描述只能猜长相）。", images=[str(dst)])
        pm.save_project(state)
        yield state, *_update_ui_with_note(
            state, f"🖼 参考图已就位（{Path(src).name}）→ 点「🧊 生成建模」按它建模")
    except Exception as e:
        yield state, *_update_ui_with_note(state, f"参考图处理失败：{e!r}")


def on_reference_search(query: str, state: dict):
    """agent 联网找参考图（2026-09-24 新增）。

    输入中文/英文角色名 → 多语查询扩写 → web_ref.find_character_image 搜图 + 抠白底
    → 落进本组 candidates/ → 用户在对话框气泡里**看到图**后**自己点 🎯 采用为参考图**。

    关键改动（2026-09-24 端到端发现）：之前这里自动写 state["ref_upload"] = dst，
    Bing 拿的图跟用户描述无关时**整条链路跑偏** ✗。现在必须用户确认（点 🎯 按钮）才标记为参考图。
    经验：Bing 对中文 IP 名（如"熊大"）会返回动物；先查英文别名（Briar Bear）才命中 IP。
    """
    from backend import web_ref as _web_ref
    if not state or not state.get("path"):
        yield state, *_update_ui_with_note(state, "还没建对话组，先发一句话再找参考图。")
        return
    q = (query or "").strip()
    if not q:
        yield state, *_update_ui_with_note(state, "输入角色名（中/英都行，英文优先）。")
        return
    try:
        yield state, *_update_ui_with_note(state, f"🔍 正在网上找「{q}」的参考图…")
        ref = _web_ref.find_character_image(q, limit=8, timeout=25)
        if not ref or not ref.get("image"):
            yield state, *_update_ui_with_note(
                state, f"未找到「{q}」的可用参考图。试试英文名（如 Briar Bear），或直接用 🖼 上传。")
            return
        proj = Path(state["path"]); cdir = proj / "candidates"; cdir.mkdir(parents=True, exist_ok=True)
        img = ref["image"]
        if img.mode != "RGB": img = img.convert("RGB")
        dst = cdir / f"ref_search_{int(time.time())}.png"
        img.save(dst, "PNG")
        cands = list(state.get("candidates") or [])
        cands.append(str(dst))
        state["candidates"] = cands
        state["selected_idx"] = len(cands) - 1
        state["selected"] = str(dst)
        # ★ 不再自动 ref_upload —— 必须用户点「🎯 采用为参考图」才标（端到端 bug A 修）
        state["ref_upload_pending"] = True
        state["phase"] = "selecting"
        conv._append_conversation(
            state, "user", f"（联网找参考图：{q}）", "")
        # 把图嵌进对话气泡 → 用户一眼看到是不是想要的
        conv._append_conversation(
            state, "assistant", "", (
                f"🌐 联网找到了 1 张候选参考图（{dst.name}）。\n\n"
                "**先看一眼是不是你想要的** —— IP 角色 Bing 不一定能拿到对的；\n"
                "如果对，点 **🎯 采用为参考图**（点错就上传你自己的）；\n"
                "如果**不对**（如搜熊大却看到动物/无关图），点 **🧊 生成建模** 改用纯文字生成。",
            ), images=[str(dst)])
        pm.save_project(state)
        yield state, *_update_ui_with_note(
            state, f"🌐 联网候选图已就位（{dst.name}）→ 看气泡里的图，点 🎯 采用为参考图 或 🧊 直接生成")
    except Exception as e:
        yield state, *_update_ui_with_note(state, f"联网找图失败：{e!r}")


def on_adopt_as_reference(state: dict):
    """把当前 selected 标为参考图（用户手动采纳 → state.ref_upload）。

    这是 on_reference_search 之后的"确认采用"步骤。没有这一步的话，Bing 拿的不相关图
    会直接进 2.1 参考图分支、整条链跑偏（端到端 bug A）。"""
    if not state or not state.get("selected"):
        yield state, *_update_ui_with_note(state, "还没图可选 — 上传或联网找一张先。")
        return
    sel = state["selected"]
    state["ref_upload"] = sel
    state.pop("ref_upload_pending", None)
    try:
        pm.save_selected_image(Path(state["path"]), Path(sel).read_bytes())
    except Exception:
        pass
    pm.save_project(state)
    conv._append_conversation(
        state, "assistant", "",
        "✅ 已采纳为参考图。下一步点 **🧊 生成建模** 就按这张图做 3D 摆件。")
    yield state, *_update_ui_with_note(state, "✅ 已采纳为参考图 → 点「🧊 生成建模」按它建模")


def on_btn_generate(state: dict):
    """生图（流式 generator：先显示进度，再显示结果）。

    文案与进度环的数字都来自 backend/timings 的**实测中位**（不再是拍脑袋的"20-40 秒"）。
    """
    import backend.timings as T
    state["phase"] = "generating"
    conv._append_conversation(state, "assistant", "",
                              f"🎨 正在生成图片（512 预览）…实测通常 {T.human(T.estimate('image_512')[0])}")
    t0 = time.time()
    yield state, *_update_ui(state, ring=ring_html("image_512"))

    # 真正生成（阻塞十几秒）
    result = conv._do_generate_images(state)
    el = time.time() - t0
    yield result["project"], *_update_ui(result["project"], ring=ring_html("image_512", el, done=True))


def on_btn_model(state: dict):
    """生成建模（流式 generator：先显示进度，再显示结果）"""
    # 用户自己按了建模 → 撤掉「自动继续」布防，否则心跳到点会再跑一次（双跑 = 白烧 GPU）
    conv.cancel_autopilot(state, "用户手动点了生成建模")
    import backend.timings as T
    state["phase"] = "3d"
    _td_key = (state.get("auto_params") or {}).get("model_3d") or _default_3d_model()
    _q = (state.get("auto_params") or {}).get("quality_3d") or "高质量"
    _stage = T.stage_for_3d(_td_key or "hunyuan21", _q)
    conv._append_conversation(state, "assistant", "",
                              f"🧊 正在 3D 建模（{_td_key}，{_q}）…历史实测通常 {T.human(T.estimate(_stage)[0])}")
    t0 = time.time()
    yield state, *_update_ui(state, ring=ring_html(_stage))

    result = conv._do_generate_3d(state)
    el = time.time() - t0
    # 建模后 state 仍留在**本组**（轮转推迟到下一条消息，见 rotate_if_finished），
    # 用 result["glb"] 把刚建好的模型交给查看器
    yield result["project"], *_update_ui(result["project"], result.get("glb"),
                                        ring=ring_html(_stage, el, done=True))


def on_btn_export_stl(state: dict):
    """导出 STL（generator）：把当前 GLB 转成可打印的 STL。

    2026-09-23 用户反馈"缺少 glb 转 stl 这一步"：以前只有两个入口 ——
    建模成功后自动导一次、或让 agent 从对话里判断 export_stl。
    两者都不成时（自动那步失败 / 改了尺寸想重导），界面上没有任何地方能点。

    2026-09-23 二次反馈"点了没转"：其实**每次都在 0.25 秒内转好了**（日志实证），
    但界面只回一句"已导出"、**不给文件在哪、也不给下载** ✗ → 用户拿不到东西 = 等于没转。
    所以这次：实测估算（2s，不再是乱写的"20-40 秒"）+ 进度环 + 文件路径 + **下载卡片**。
    """
    import backend.timings as T
    if not (state or {}).get("glb_path"):
        yield state, *_update_ui_with_note(state, "还没有 3D 模型，先生成建模再导 STL。")
        return
    t0 = time.time()
    med, hi = T.estimate("stl")
    yield (state, *_update_ui_with_note(
        state,
        f"📦 正在把 GLB 转成 STL（水密化 + 按尺寸缩放）…实测通常 {T.human(med)}，"
        f"超大件最长见过 {T.human(hi)}",
        ring=ring_html("stl")))
    result = conv._do_export_stl(state)
    prj = result["project"]
    el = time.time() - t0
    stl = result.get("stl_path") or prj.get("stl_path") or ""
    # 注意按钮类的 yield 形状是 (state, chat, hero, …) —— state **在最前**（接线是
    # outputs=[state] + ui_outputs）。on_submit 那种 ("", state, …) 的形状在这里会把 "" 写进 state。
    reply = (result.get("reply") or "").strip() or f"STL 已导出（{T.human(el)}）"
    # 文件卡片不用在这里单独返回：state["stl_path"] 已写好，_update_ui 会按它显示卡片
    yield (prj, *_update_ui_with_note(prj, reply, ring=ring_html("stl", el, done=True)))
    yield (prj, *_update_ui(prj, ring=ring_html("stl", el, done=True)))


def on_upload_and_edit(file_obj, prompt: str, state: dict):
    """一次事件完成「上传参考图 + 按提示词改图」。

    站内 React 端不像 Gradio 页面那样有持久 gr.State，
    upload 和 edit 分成两个事件会丢 ref_upload；合并成一个事件最稳。
    """
    st = state
    if file_obj is not None:
        for out in on_reference_uploaded(file_obj, state):
            st = out[0]
    yield from on_edit_with_prompt(prompt, st)


def on_edit_with_prompt(prompt: str, state: dict):
    """上传图片 + 提示词 → 按提示词改图；不自动建模，用户再点「生成建模」。"""
    if state is None:
        state = {}
    src = state.get("ref_upload") or state.get("selected")
    text = (prompt or "").strip()
    if not src or not Path(src).exists():
        yield state, *_update_ui_with_note(state, "先点「🖼 上传图片」选一张图，再写要改什么。")
        return
    if not text:
        yield state, *_update_ui_with_note(state, "请在图片旁边写一句要改什么，比如「改成蓝色」「加一对翅膀」。")
        return
    try:
        from backend.conversation import _apply_refinement
        _apply_refinement(state, text)
    except Exception as e:
        print(f"[DSH] edit prompt apply failed: {e!r}", flush=True)
    state.setdefault("req_notes", []).append(text)
    state["ref_upload"] = src
    conv.cancel_autopilot(state, "用户按提示词改图")
    import backend.timings as T
    t0 = time.time()
    yield state, *_update_ui(state, ring=ring_html("image_512"))
    try:
        result = conv._do_generate_images(state)
        prj = result.get("project", state)
        # 改图后给出预览，不自动建模；用户确认后再点生成建模
        conv.cancel_autopilot(prj, "改图后等待用户确认")
        el = time.time() - t0
        yield prj, *_update_ui(prj, ring=ring_html("image_512", el, done=True))
    except Exception as e:
        yield state, *_update_ui_with_note(state, f"改图失败：{e!r}")


def on_btn_edit(state: dict):
    """修改图片（2026-09-25 修：同步函数 → generator，否则 Gradio 不响应）"""
    if state is None:
        state = {}
    result = conv.handle_user_input("修改图片")
    state = result.get("project", state)
    yield state, *_update_ui(state)


def on_btn_new(state: dict):
    """新建对话组（generator：必须 yield 才能让 Gradio 拿到 state + UI 元组）。

    2026-09-25 修：之前是同步函数 `return result["project"]`，Gradio 看签名发现
    无 *args **kwargs 且只 1 个 dict 参数 → 它把 inputs/outputs 都吞了 → 实际 API
    `parameters=[] returns=[]`，点按钮什么也没发生（用户报"卡住没新建"）。
    改成 generator yield (state, *14 UI) → outputs=[state] + ui_outputs 一次性接住。
    """
    if state is None:
        state = {}
    # 换组了 → 旧组的「自动继续」布防作废（否则心跳会在新组里替他建模）
    conv.cancel_autopilot(state, "用户新建了对话组")
    result = conv._do_new_project()
    state = result["project"]
    # 新组欢迎气泡
    if result.get("reply"):
        conv._append_conversation(state, "assistant", "", result["reply"])
        pm.save_project(state)
    yield state, *_update_ui_with_note(state, "🆕 新对话组已创建 → 输入想做什么")


def arm_heartbeat(state: dict):
    """布防后打开「自动继续」心跳；没布防就保持关闭。

    单独挂到 on_submit / on_btn_generate 之后的 .then(...) —— 这样不用去改它们
    内部那一堆流式 yield 的形状（每个 yield 都要多带一个 timer 值，很容易漏 ✗）。
    空闲时绝不能让心跳开着：Gradio 会为每个 tick 事件显示"处理中"，界面就像一直在弹。
    """
    return gr.Timer(active=bool((state or {}).get("auto_deadline")))


def on_typing(state: dict):
    """用户**开始打字**就取消「自动继续」（未提交也算提要求），并关掉心跳。

    这是宇凯那句"5 秒内没有输入修改需求"的直接翻译：他在敲字 → 让位。
    走 save=False：每个按键都落盘会把磁盘写爆，而且刷新后布防丢失只是边缘情况。
    """
    if state and state.get("auto_deadline"):
        conv.cancel_autopilot(state, "用户正在输入", save=False)
        print("[DSH] 自动继续已取消：用户正在输入")
    return state, gr.Timer(active=False)


def on_switch_project(folder_name: str):
    proj = conv.switch_project(folder_name)
    return proj


def refresh_history():
    projects = conv.list_projects()
    choices = [p["folder"] for p in projects]
    labels = [f'{p["folder"][:19]} — {p["user_prompt"][:24] if p["user_prompt"] else "新对话"} [{p["phase"]}]' for p in projects]
    current=(conv.current_project or {}).get('folder')
    return gr.update(choices=choices, value=current if current in choices else None)


HERO_HTML = """
<div class="empty-hero">
  <h2>从一句话到<span class="accent">可打印的 3D 实体</span></h2>
  <p>描述你想要的物品 → 生成图片 → 一键重建 3D → 导出 STL 直接打印</p>
  <div class="chips">
    <div class="chip">我要一个熊大</div>
    <div class="chip">帮我设计一个机械手臂底座</div>
    <div class="chip">画一个茶杯垫，直径 90mm</div>
    <div class="chip">做一个手机支架，蓝色</div>
  </div>
</div>
"""


def on_autopilot_tick(state: dict):
    """「自动继续」心跳：**延时一次性**触发，跳完自己关掉。

    怎么走到这一步的（值得记）：
      · 第一版：常驻 1 秒一跳 → Gradio 每秒都显示"处理中"加载指示，用户看到的是
        **界面一直在弹** ✗（宇凯报的就是这个）
      · 现在：Timer 平时 inactive；布防时由布防方打开（value=delay 秒），
        到点跳**一次** → 到这里判断该不该动手 → 无论结果如何都把自己关掉。
        所以每次布防只有 1 个事件，空闲时 0 个事件。
    输出元数 = 1(state) + 11(UI) + 1(auto_timer)，timer 用来关自己。
    """
    if not (state or {}).get("auto_deadline"):
        yield (state, *[gr.update() for _ in range(14)], gr.Timer(active=False))
        return
    due, why = conv.autopilot_due(state)
    if not due:
        # 还没到期（例如用户把 delay 配置改大了，而 Timer 仍按旧值跳）→ 继续等，
        # 但把 Timer 关掉，避免它按 1 秒/旧周期反复触发把界面闪花
        print(f"[DSH] 自动继续未到期：{why} → 心跳休眠")
        yield (state, *[gr.update() for _ in range(14)], gr.Timer(active=False))
        return
    # 到点：先明确告诉用户发生了什么（不能默默开始烧 GPU）
    yield (state, *_update_ui_with_note(state, "⏱ 没有修改需求，自动开始建模（快速档，约 2-3 分钟）…"),
           gr.Timer(active=False))
    res = conv.autopilot_fire(state)
    st = res.get("project") or state
    reply = res.get("reply") or "已自动建模完成"
    yield (st, *_update_ui_with_note(st, reply, res.get("glb")), gr.Timer(active=False))


def _update_ui(state: dict, glb_override: str = None, ring: str = None):
    """返回 13 个值：chat, hero, viewer, 6 个按钮, sidebar, history, 环形进度条, STL 文件卡片。
    第 12/13 个是本轮加的：进度环、以及**按当前组状态**决定的 STL 下载卡片
    （后者必须在这里返回 —— 否则换组后它仍挂着上一组的文件，用户会说"点新建对话会串"）。
    改这里务必同步 ui_outputs。"""
    phase = state.get("phase", "empty")
    conv_list = state.get("conversation", [])
    chat = _chat_value(conv_list)
    btns = _buttons_for_phase(phase, state)
    # 欢迎页只在「还没有任何对话」时显示
    hero = gr.update(visible=not conv_list)
    # 3D 查看器：只在「这一组真的产出了模型」时展示。
    # 优先级：显式 override（刚建模完，模型已归到归档组）→ 本项目 glb_path。
    # 2026-09-22 修：不再回退到 last_glb —— 那会把上一组的模型带进新组，
    # 用户一发消息（新组还没建任何模型）就弹出展示框。
    glb = glb_override or state.get("glb_path") or ""
    has_glb = bool(glb) and Path(glb).exists()
    viewer = gr.update(value=glb if has_glb else None, visible=has_glb)
    # 2026-09-22：去掉候选图 gallery（图已在对话气泡里内嵌显示，gallery 是重复的）
    stl_path = state.get("stl_path") or ""
    stl_ok = bool(stl_path) and Path(stl_path).exists()
    return (
        chat,
        hero,
        viewer,
        btns["refine"], btns["gen"], btns["edit"], btns["model"], btns["new"], btns["stl"], btns["adopt"],
        build_sidebar(state),
        refresh_history(),
        ring if ring is not None else ring_html(""),      # 第 12 个：环形进度条（空闲时留白）
        # 第 13 个：STL 文件卡片 —— 只显示**当前组**的 STL。没导出过就不画框
        # （gr.File 即使 value=None 也会画空框，用户会觉得"这东西一直在那里" ✗）
        gr.update(value=stl_path if stl_ok else None, visible=stl_ok),
    )


# ══════════════════════════════════════════════════════════════════
# 布局
# ══════════════════════════════════════════════════════════════════
def build():
    initial = conv.current_project
    initial_msg = conv.get_initial_message()
    projects = conv.list_projects()
    hist_choices = [p["folder"] for p in projects]
    # 根据初始阶段设置按钮初始可见性
    _init_btns = _buttons_for_phase(initial.get("phase", "empty"), initial)
    # 初始就有 GLB（打开历史项目）→ 直接展示
    _init_glb = initial.get("glb_path", "") or ""
    _init_glb_ok = bool(_init_glb) and Path(_init_glb).exists()
    _init_stl = initial.get('stl_path') or ''
    _init_stl_ok = bool(_init_stl) and Path(_init_stl).is_file()

    with gr.Blocks(title="铸想 ForgeForm", css=CSS,
                   head='<script>setInterval(function(){var e=document.querySelector("#ref_up");if(e){e.style.color="#e6ecf7";e.querySelectorAll("*").forEach(function(x){x.style.setProperty("color","#e6ecf7","important")})}},500)</script>',
                   theme=THEME) as app:
        state = gr.State(initial)

        # ── 顶部导航条 ──────────────────────────────────
        with gr.Row(elem_classes="navbar"):
            gr.HTML(
                '<div class="nav-logo"><span class="dot"></span>铸想</div>'
            )
            history_dd = gr.Dropdown(
                choices=hist_choices,
                value=initial.get("folder", ""),
                label="",
                elem_id="history_dd",
                show_label=False,
                scale=2,
                min_width=220,
            )
            btn_new_top = gr.Button("＋ 新建", variant="secondary", scale=1, min_width=96)

        # ── 主体：左侧对话区（宽）+ 右侧侧边栏（窄）────
        with gr.Row(elem_classes="main-wrap"):
            # 对话区（宽，占主视觉）
            with gr.Column(scale=7, min_width=460, elem_classes="chat-col"):
                # 欢迎页（空对话时显示）
                hero = gr.HTML(HERO_HTML, visible=not bool(initial.get("conversation")),
                               elem_classes="hero-block")

                chatbot = gr.Chatbot(
                    label="",
                    value=_chat_value(initial.get('conversation') or []) or [[None, initial_msg]],
                    show_label=False,
                    elem_id="chatbox",
                    height=470,
                )

                # 3D 查看器（旧版同款 gr.Model3D，v3 改版漏移植）：
                # 建模完成后在这里展示 GLB，可拖动旋转/缩放
                viewer = gr.Model3D(
                    label="🧊 3D 模型（拖动旋转 · 滚轮缩放）",
                    value=_init_glb if _init_glb_ok else None,
                    visible=_init_glb_ok,
                    height=320,
                    elem_id="glb_viewer",
                )

                # 动作按钮行（对话里紧跟的交互按键，初始按阶段显示）
                with gr.Row(elem_classes="action-bar"):
                    btn_refine = gr.Button("💬 继续细化要求", variant="secondary", visible=_init_btns["refine"]["visible"], elem_id="btn_refine")
                    btn_gen = gr.Button("🎨 生成图片", variant="primary", visible=_init_btns["gen"]["visible"], elem_id="btn_gen")
                    btn_edit = gr.Button("🔧 修改图片", variant="secondary", visible=_init_btns["edit"]["visible"], elem_id="btn_edit")
                    btn_model = gr.Button("🧊 生成建模", variant="primary", visible=_init_btns["model"]["visible"], elem_id="btn_model")
                    btn_new = gr.Button("🏠 新建对话组", variant="secondary", visible=_init_btns["new"]["visible"], elem_id="btn_new")
                    btn_stl = gr.Button("📦 导出 STL", variant="secondary", visible=_init_btns["stl"]["visible"], elem_id="btn_stl")
                    # 「🎯 采用为参考图」：联网找图后必须确认 → 端到端 bug A 修
                    btn_adopt = gr.Button("🎯 采用为参考图", variant="primary",
                                         visible=_init_btns.get("adopt", {"visible": False})["visible"],
                                         elem_id="btn_adopt")

                # 环形进度条（长步骤的实测估算）。空闲时是一块留白，不占视觉。
                progress = gr.HTML(ring_html(""), elem_id="progress_ring")
                # 导出 STL 后在这里给一个可点下载/可拖进切片软件的文件卡片
                # 默认 hidden：gr.File 即使 value=None 也会画一个空框，用户会觉得"一直在那里" ✗
                # 只有当前组真有 STL 时才显示（值由 _update_ui 按 state["stl_path"] 决定）
                stl_file = gr.File(label="📦 STL 文件（下载 / 拖进 Bambu）", value=_init_stl if _init_stl_ok else None,
                                   visible=_init_stl_ok, elem_id="stl_file")

                # 输入框（2026-09-25 精简）：只留聊天框+发送+上传，三件小而清晰
                with gr.Row(elem_classes="input-row input-row-chat"):
                    msg = gr.Textbox(
                        placeholder="描述你想打印什么…",
                        lines=1,
                        scale=11,
                        elem_id="chat_input",
                        show_label=False,
                        container=False,
                    )
                    send = gr.Button("发送", variant="primary", scale=1, elem_id="send_btn")
                with gr.Row(elem_classes="input-row input-row-upload"):
                    ref_up = gr.UploadButton("🖼 上传图片", file_types=["image"],
                                             size="sm", scale=0, min_width=100, elem_id="ref_up")
                    edit_prompt = gr.Textbox(
                        placeholder="上传图片后，描述要改什么（例：改成蓝色 / 加一对翅膀）…",
                        lines=1, scale=8, show_label=False, container=False,
                        elem_id="edit_prompt",
                    )
                    btn_edit_submit = gr.Button("按提示词修改", variant="secondary",
                                                scale=2, elem_id="btn_edit_submit")

                # 「自动继续」心跳：**默认不跳**（active=False）。
                # 2026-09-23 踩坑：一开始写成常驻 active=True + 1 秒一跳，结果 Gradio 每秒
                # 都在这个事件上显示"处理中"加载指示，用户在页面上看到的就是**一直在弹** ✗。
                # 现在只有"图片就绪、布防生效"那几秒里才由布防方打开，跳完立刻自己关掉。
                # 延时一次性心跳：value = 配置里的等待秒数（布防后跳一次就自关）。
                # 绝不能用常驻 1 秒一跳 —— Gradio 会为每个事件显示"处理中"，界面看着一直在弹 ✗
                auto_timer = gr.Timer(value=float(conv.autopilot_cfg()["delay_sec"]), active=False)

            # 侧边栏（窄）
            with gr.Column(scale=3, min_width=260, elem_classes="side-col"):
                sidebar = gr.HTML(build_sidebar(initial), elem_id="sidebar")

        # ── 事件绑定 ──────────────────────────────────────
        # 12 个：chat, hero, viewer, 6 按钮, sidebar, history, **进度环**
        # （改这里必须同步 _update_ui 的返回值元数）
        # 14 个：chat, hero, viewer, 7 按钮, sidebar, history, 进度环, STL 文件卡片
        # 「🎯 采用」是 selecting 阶段新加的（联网找图后用户必须确认 → 端到端 bug A 修）
        ui_outputs = [chatbot, hero, viewer, btn_refine, btn_gen, btn_edit, btn_model, btn_new,
                      btn_stl, btn_adopt, sidebar, history_dd, progress, stl_file]

        send.click(fn=on_submit, inputs=[msg, state], outputs=[msg, state] + ui_outputs).then(
            fn=arm_heartbeat, inputs=[state], outputs=[auto_timer])
        msg.submit(fn=on_submit, inputs=[msg, state], outputs=[msg, state] + ui_outputs).then(
            fn=arm_heartbeat, inputs=[state], outputs=[auto_timer])

        # 上传参考图：直接进"已选图"状态（phase=selecting）→ 点生成建模就按它建模
        ref_up.upload(fn=on_reference_uploaded, inputs=[ref_up, state],
                      outputs=[state] + ui_outputs)

        # 上传图片 + 提示词 → 改图；不自动建模
        btn_edit_submit.click(fn=on_upload_and_edit, inputs=[ref_up, edit_prompt, state],
                              outputs=[state] + ui_outputs)

        # 2026-09-25：去掉了「🌐 联网找」入口（界面精简）——
        # 想联网找就点 🖼 上传按钮旁边的"联网找"提示（todo）。

        # 联网找图后让用户确认"是不是想要这张" → 才标为 ref_upload
        btn_adopt.click(fn=on_adopt_as_reference, inputs=[state],
                        outputs=[state] + ui_outputs)

        # 「自动继续」心跳接线：outputs = state + 11 个 UI + timer（timer 让心跳能关自己）。
        # 布防方（on_submit / on_btn_generate）负责把它打开，所以那两个事件也要带 timer 输出。
        auto_timer.tick(fn=on_autopilot_tick, inputs=[state], outputs=[state] + ui_outputs + [auto_timer])

        # 用户一开始打字就取消「自动继续」（"5 秒内没有输入修改需求"里的"输入"）
        # trigger_mode="always_last"：打字停下后才发一次（每次按键都发会让界面一直闪 ✗）
        msg.input(fn=on_typing, inputs=[state], outputs=[state, auto_timer],
                  trigger_mode="always_last")

        btn_refine.click(fn=on_btn_refine, inputs=[state], outputs=[state] + ui_outputs)
        # generator 事件：outputs 需含 state + 所有 UI
        btn_gen.click(fn=on_btn_generate, inputs=[state], outputs=[state] + ui_outputs).then(
            fn=arm_heartbeat, inputs=[state], outputs=[auto_timer])   # 生图后会布防
        btn_edit.click(fn=on_btn_edit, inputs=[state], outputs=[state] + ui_outputs)
        btn_model.click(fn=on_btn_model, inputs=[state], outputs=[state] + ui_outputs).then(
            fn=arm_heartbeat, inputs=[state], outputs=[auto_timer])   # 已取消 → 心跳关掉
        btn_stl.click(fn=on_btn_export_stl, inputs=[state], outputs=[state] + ui_outputs)
        btn_new.click(fn=on_btn_new, inputs=[state], outputs=[state] + ui_outputs)
        btn_new_top.click(fn=on_btn_new, inputs=[state], outputs=[state] + ui_outputs)

        history_dd.input(fn=on_switch_project, inputs=[history_dd], outputs=[state]).then(
            fn=_update_ui, inputs=[state], outputs=ui_outputs)

    return app


def register_model_library(sa) -> None:
    """把 backend/model_library 挂到 FastAPI（2026-10-19：替掉原来的占位路由）。

    读写分离：
      · 浏览 / 预览 / 下载：公开 —— 本来就只是本机产物；
      · 发布 / 删除：必须带 `X-Ops-Token`（= DSH_OPS_TOKEN）——
        发布能把**任意本地文件**复制进库，删除会真删文件，不能匿名开放。
    """
    from fastapi import Header, HTTPException
    from fastapi.responses import FileResponse, HTMLResponse
    from backend import model_library as ml
    import os
    import secrets

    _token = (os.environ.get("DSH_OPS_TOKEN") or "").strip()

    def _need_token(value: str | None) -> None:
        # 没配 token 就一律拒绝（而不是"没配就放行"）
        if not _token or not value or not secrets.compare_digest(value, _token):
            raise HTTPException(status_code=403, detail="需要有效的 X-Ops-Token（DSH_OPS_TOKEN）")

    def _media_type(path: str) -> str:
        return {
            "stl": "model/stl",
            "glb": "model/gltf-binary",
            "gltf": "model/gltf+json",
        }.get(Path(path).suffix.lower().lstrip("."), "application/octet-stream")

    @sa.get("/model-library", response_class=HTMLResponse)
    def _ml_index():
        return ml.render_library_html(ml.list_models(limit=200), ml.get_stats())

    @sa.get("/model-library/preview/{model_id}", response_class=HTMLResponse)
    def _ml_preview(model_id: str):
        m = ml.get_model(model_id)
        if not m:
            raise HTTPException(status_code=404, detail="模型不存在")
        ml.increment_views(model_id)
        return ml.render_preview_html(m)

    @sa.get("/model-library/file/{model_id}")
    def _ml_file(model_id: str):
        m = ml.get_model(model_id)
        if not m or not Path(m["file_path"]).is_file():
            raise HTTPException(status_code=404, detail="模型文件不存在")
        return FileResponse(m["file_path"], media_type=_media_type(m["file_path"]))

    @sa.get("/model-library/download/{model_id}")
    def _ml_download(model_id: str):
        m = ml.get_model(model_id)
        if not m or not Path(m["file_path"]).is_file():
            raise HTTPException(status_code=404, detail="模型文件不存在")
        ml.increment_dls(model_id)
        return FileResponse(
            m["file_path"],
            media_type=_media_type(m["file_path"]),
            filename=Path(m["file_path"]).name,
        )

    @sa.post("/model-library/publish")
    def _ml_publish(
        payload: dict,
        x_ops_token: str | None = Header(default=None, alias="X-Ops-Token"),
    ):
        _need_token(x_ops_token)
        body = payload or {}
        res = ml.publish(
            body.get("file_path") or "",
            name=body.get("name") or "",
            prompt=body.get("prompt") or "",
            description=body.get("description") or "",
            tags=body.get("tags") or "",
        )
        if not res.get("ok"):
            raise HTTPException(status_code=400, detail=res.get("error") or "发布失败")
        return res

    @sa.delete("/model-library/{model_id}")
    def _ml_delete(
        model_id: str,
        x_ops_token: str | None = Header(default=None, alias="X-Ops-Token"),
    ):
        _need_token(x_ops_token)
        if not ml.delete_model(model_id):
            raise HTTPException(status_code=404, detail="模型不存在")
        return {"ok": True, "id": model_id}


if __name__ == "__main__":
    app = build()
    app.launch(
        server_name="0.0.0.0",
        server_port=int(os.environ.get("FOUNDRY_PORT", "7860")),
        share=False,
        allowed_paths=[
            str(ROOT / "outputs"),
            str(ROOT / "temp"),
            str(ROOT / "projects"),
            str(COMFYUI_DIR / "output"),
            str(COMFYUI_DIR / "input"),
        ],
        prevent_thread_lock=True,
    )
    _sa = app.server_app
    from backend.viewer_compat import register_viewer_compat
    register_viewer_compat(_sa)
    register_model_library(_sa)
    print("[DSH] 自定义路由注册完成: /model-library（列表/预览/下载 + token 保护的发布/删除）")

    # ══════════════════════════════════════════════════════════════
    # 运维接口：按对话组重跑「重出图 / 重建模」（2026-10-17 新增）
    # ══════════════════════════════════════════════════════════════
    #
    # ★ 为什么要这个接口
    #   宇凯要求："帮他们没做完的做完，做完的检查质量不好的重做"。
    #   但铸想的建模入口（on_btn_*）走 **Gradio 会话级 gr.State**，
    #   而 current_project 是**进程级全局** —— 从外部调 on_btn_* 
    #   会作用到"当前会话的当前组"，不是我们想补的那个组 ✗
    #
    #   所以这里开一个直连后端的接口：自己 load_project(组名) 拿 state，
    #   调 _do_generate_3d / 生图，全程**不碰** Gradio 的会话状态 ✓
    #
    # ★ 安全：必须带 token（环境变量 DSH_OPS_TOKEN）。
    #   没配 token 就**一律拒绝** —— 不给"忘了配就裸奔"的机会。
    #   这个接口能让任意组重新占用 GPU，绝不能匿名开放。
    import os as _os
    import secrets as _secrets
    import datetime as _datetime
    from fastapi import Header as _Header
    from fastapi.responses import JSONResponse as _JSON

    _OPS_TOKEN = (_os.environ.get("DSH_OPS_TOKEN") or "").strip()

    def _check_ops(token: str | None) -> bool:
        if not _OPS_TOKEN:
            return False            # 没配 → 不允许（而不是允许）
        return bool(token) and _secrets.compare_digest(token, _OPS_TOKEN)

    # ── 异步任务表（进程内，够用）──────────────────────────────
    #
    # ★ 为什么要异步（2026-10-17 实测踩到）
    #   菠萝那次「重出图 + 高清重绘 + 双阶段 3D」跑了约 25 分钟，
    #   我的 curl 设在 1500 秒（正好 25 分钟）→ EXIT=28 超时放弃，
    #   但**服务端其实还在跑**（后来文件确实出来了）。
    #   同步接口等长任务必然踩这个坑：调用方超时 ≠ 任务失败，
    #   很容易被误判成"失败了"去重跑一遍，白烧一次 GPU ✗
    #
    #   改成：POST 立刻返回 job_id，另开线程干活，GET 轮询进度 ✓
    #   这样无论跑多久，调用方都能安心拿结果。
    import threading as _threading
    import uuid as _uuid

    _OPS_JOBS: dict = {}
    _OPS_LOCK = _threading.Lock()

    def _ops_run(job_id: str, folder: str, regen: bool) -> None:
        import backend.project_manager as _pm2
        try:
            state = _pm2.load_project(folder)
            conv2 = globals()["conv"]
            if regen:
                with _OPS_LOCK:
                    _OPS_JOBS[job_id]["step"] = "image"
                print(f"[OPS] {job_id[:8]} 重出图: {folder}")
                r = conv2._do_generate_images(state)
                state = r.get("project", state)
                # ★★ 必须立刻取消"自动继续"定时器（2026-10-17 实测踩到）★★
                #
                #   铸想在出图后会布防一个 "N 秒后若无修改 → 自动建模" 的定时器
                #   （见 _do_generate_images 里的 arm_autopilot）。
                #   我们**紧接着自己会调 _do_generate_3d** —— 于是同一个组建模两次：
                #     实测日志出现 "3D 任务已提交: 43671b76" 之后又来 "330c708f"，
                #     两个任务在 ComfyUI 里打架，job 卡在 running/3d 二十多分钟 ✗
                #
                #   取消掉它：这条路我们自己管建模时机，不需要自动继续。
                try:
                    conv2.cancel_autopilot(state, why="运维补做：马上自己建模")
                    print(f"[OPS] {job_id[:8]} 已取消自动继续定时器（避免重复建模）")
                except Exception as _ce:
                    print(f"[OPS] ⚠️ 取消自动继续失败（继续执行）: {_ce!r}")

            with _OPS_LOCK:
                _OPS_JOBS[job_id]["step"] = "3d"
            print(f"[OPS] {job_id[:8]} 重建模: {folder}")
            r = conv2._do_generate_3d(state)
            state = r.get("project", state)

            files = sorted(_os.listdir(state.get("path") or "."))
            with _OPS_LOCK:
                _OPS_JOBS[job_id].update({
                    "state": "done",
                    "step": "finished",
                    "has_glb": "model.glb" in files,
                    "has_stl": "model.stl" in files,
                    "has_preview": "preview.png" in files,
                    "reply": str(r.get("reply", ""))[:300],
                })
        except Exception as e:
            with _OPS_LOCK:
                _OPS_JOBS[job_id].update({"state": "error", "error": f"{type(e).__name__}: {e}"})
            print(f"[OPS] ✗ {folder} 失败: {e!r}")

    @_sa.post("/ops/rebuild")
    def _ops_rebuild_start(payload: dict, x_ops_token: str = _Header(default=None)):
        """**提交**一次补做/重做，立刻返回 job_id（不等结果）。

        body: {"project": "...", "regen_image": true|false}
        → {"ok": true, "job": "abc123", "state": "running"}
        轮询：GET /ops/rebuild/<job>? 见下面
        """
        if not _check_ops(x_ops_token):
            return _JSON({"ok": False, "error": "unauthorized"}, status_code=401)

        folder = str((payload or {}).get("project") or "").strip()
        if not folder:
            return _JSON({"ok": False, "error": "missing project"}, status_code=400)

        import backend.project_manager as _pm
        try:
            _dir = _pm.PROJECTS_DIR / folder
            if not _dir.is_dir():
                return _JSON({"ok": False, "error": f"project not found: {folder}"}, status_code=404)
        except Exception as e:
            return _JSON({"ok": False, "error": f"bad project path: {e!r}"}, status_code=400)

        regen = bool((payload or {}).get("regen_image"))
        state0 = _pm.load_project(folder)
        if not regen and not state0.get("selected") and not (state0.get("candidates") or []):
            return _JSON({"ok": False, "error": "no image in project; set regen_image=true"},
                         status_code=400)

        job_id = _uuid.uuid4().hex[:12]
        with _OPS_LOCK:
            _OPS_JOBS[job_id] = {
                "state": "running", "project": folder, "step": "starting",
                "regen_image": regen,
                "created": _datetime.datetime.now().isoformat(timespec="seconds"),
            }
        _threading.Thread(target=_ops_run, args=(job_id, folder, regen), daemon=True).start()
        return _JSON({"ok": True, "job": job_id, "state": "running", "project": folder})

    @_sa.get("/ops/rebuild/{job_id}")
    def _ops_rebuild_status(job_id: str, x_ops_token: str = _Header(default=None)):
        if not _check_ops(x_ops_token):
            return _JSON({"ok": False, "error": "unauthorized"}, status_code=401)
        with _OPS_LOCK:
            job = dict(_OPS_JOBS.get(job_id) or {})
        if not job:
            return _JSON({"ok": False, "error": "unknown job"}, status_code=404)
        job["ok"] = job.get("state") != "error"
        job["job"] = job_id
        return _JSON(job)

    @_sa.get("/ops/jobs")
    def _ops_list(x_ops_token: str = _Header(default=None)):
        if not _check_ops(x_ops_token):
            return _JSON({"ok": False, "error": "unauthorized"}, status_code=401)
        with _OPS_LOCK:
            return _JSON({"ok": True, "jobs": {k: dict(v) for k, v in _OPS_JOBS.items()}})

    print("[DSH] 运维接口注册完成: POST /ops/rebuild（异步，返回 job_id）"
          " + GET /ops/rebuild/{job_id} + GET /ops/jobs"
          + ("" if _OPS_TOKEN else "（⚠️ 未配 DSH_OPS_TOKEN，该接口会一律拒绝）"))

    app.block_thread()
