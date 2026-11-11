"""项目管理器：创建/读取/存档对话组文件夹"""
import json, shutil, time
from pathlib import Path
from datetime import datetime

PROJECTS_DIR = Path(__file__).parent.parent / "projects"


def now_folder_name() -> str:
    return datetime.now().strftime("%Y-%m-%d_%H%M%S")


def create_project() -> dict:
    """创建新对话组文件夹，返回完整 state"""
    folder_name = now_folder_name()
    folder = PROJECTS_DIR / folder_name
    # 目录名只到「秒」：同一秒内连建两个组会撞名，mkdir(exist_ok=True) 会**静默合并成一个组**
    # （表现为"新建了对话组但内容还在老组里"，轮转也跟着失效）。撞了就加序号，保证是新组。
    n = 2
    while folder.exists():
        folder = PROJECTS_DIR / f"{folder_name}_{n}"
        n += 1
    folder_name = folder.name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "candidates").mkdir(exist_ok=True)

    state = {
        "folder": folder_name,
        "path": str(folder),
        "phase": "empty",
        "user_prompt": "",
        "understood": "",
        "auto_params": {},
        "conversation": [],
        "candidates": [],
        "selected": "",
        "selected_idx": -1,
        "glb_path": "",
        "stl_path": "",
        "stl_size": 120,
        "meta": {"created": time.time()},
    }
    _write_meta(folder, state)
    _save_conversation(folder, [])
    return state


def _write_meta(folder: Path, data: dict):
    with open(folder / "meta.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _read_meta(folder: Path) -> dict:
    try:
        with open(folder / "meta.json", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def list_projects() -> list[dict]:
    """返回所有对话组，按时间倒序"""
    if not PROJECTS_DIR.exists():
        return []
    projects = []
    for folder in sorted(PROJECTS_DIR.iterdir(), reverse=True):
        if folder.is_dir():
            meta = _read_meta(folder)
            # 从 meta 或文件夹名推断状态
            phase = meta.get("phase", "unknown")
            created = meta.get("created", folder.stat().st_mtime)
            # 取第一条用户消息做摘要
            user_prompt = meta.get("user_prompt", "")
            projects.append({
                "folder": folder.name,
                "path": str(folder),
                "phase": phase,
                "created": created,
                "user_prompt": user_prompt[:40] if user_prompt else "新对话",
            })
    projects.sort(key=lambda project: float(project['created']), reverse=True)
    return projects


def load_project(folder_name: str) -> dict:
    """加载整个项目的完整状态

    `folder_name` 为空时**不能往下拼路径** —— `PROJECTS_DIR / None` 会抛
    `TypeError: unsupported operand type(s) for /: 'WindowsPath' and 'NoneType'`
    （2026-09-23 app 日志里现场抓到：Gradio 下拉框刷新后传 None）。空值一律当"没有指定"，
    返回新建项目，与"文件夹不存在"同一条路。
    """
    if not folder_name or not str(folder_name).strip():
        print("[pm] load_project 收到空 folder_name → 按新建项目处理")
        return create_project()
    folder = PROJECTS_DIR / folder_name
    if not folder.exists():
        # 文件夹不存在时创建一个新的（返回完整 state）
        return create_project()
    meta = _read_meta(folder)
    conversation = _load_conversation(folder)
    return {
        "folder": folder_name,
        "path": str(folder),
        "phase": meta.get("phase", "confirm"),
        "user_prompt": meta.get("user_prompt", ""),
        "understood": meta.get("understood", ""),
        "conversation": conversation,
        "candidates": _list_candidates(folder),
        "selected": _get_selected(folder),
        "auto_params": meta.get("auto_params") or {},   # 2026-09-22：原先不返回此键
        "req_notes": meta.get("req_notes") or [],       # 需求聊天多轮上下文
        "base_prompt": meta.get("base_prompt") or meta.get("user_prompt", ""),
        "refinements": meta.get("refinements") or [],
        "glb_path": _get_file(folder, "glb"),
        "stl_path": _get_file(folder, "stl"),
        "stl_size": meta.get("stl_size", 120),
        "pending_rotate": bool(meta.get("pending_rotate")),
        "ref_upload_pending": bool(meta.get("ref_upload_pending")),   # 建模完待轮转（见 rotate_if_finished）
        "ref_upload": meta.get("ref_upload") or "",           # 用户上传的参考图（→ 生图走 Qwen-Image-2.1 分支）
        # 自动继续（宇凯 2026-09-23）：布防到期时刻/用哪个 3D 模型/布防原因。
        # 必须落盘 —— 否则刷新页面或重启后布防丢失（pending_rotate 当初就是这么漏的）。
        "auto_deadline": float(meta.get("auto_deadline") or 0),
        "auto_model": meta.get("auto_model", ""),
        "auto_reason": meta.get("auto_reason", ""),
        "auto_fired": meta.get("auto_fired", ""),
        "meta": meta,
    }


def save_project(state: dict):
    """保存项目状态到文件夹"""
    folder = PROJECTS_DIR / state["folder"]
    folder.mkdir(parents=True, exist_ok=True)

    meta = state.get("meta", {})
    meta["phase"] = state.get("phase", "confirm")
    meta["user_prompt"] = state.get("user_prompt", "")
    meta["understood"] = state.get("understood", "")
    meta["stl_size"] = state.get("stl_size", 120)
    # 2026-09-22：auto_params（颜色/尺寸/风格/模型）原先不落盘，
    # 重启后丢失 → 细化/生图回退默认值。一并持久化。
    if state.get("auto_params"):
        meta["auto_params"] = state["auto_params"]
    # 需求聊天记录（多轮上下文）：重启后仍能接着聊、总结不丢
    if state.get("req_notes"):
        meta["req_notes"] = state["req_notes"]
    if state.get("base_prompt"):
        meta["base_prompt"] = state["base_prompt"]
    if state.get("refinements"):
        meta["refinements"] = state["refinements"]
    # 建模完成待轮转标记：漏了它，重启后这一组就永远不会轮转（模型会一直挂在同组）
    meta["pending_rotate"] = bool(state.get("pending_rotate"))
    meta["ref_upload_pending"] = bool(state.get("ref_upload_pending"))
    meta["ref_upload"] = state.get("ref_upload") or ""      # 参考图路径（否则重启后"照图生成"失效）
    # 自动继续：布防到期时刻/模型/原因（同上，不落盘就会"刷新后布防消失"）
    meta["auto_deadline"] = float(state.get("auto_deadline") or 0)
    meta["auto_model"] = state.get("auto_model", "")
    meta["auto_reason"] = state.get("auto_reason", "")
    meta["auto_fired"] = state.get("auto_fired", "")
    # ★★ 2026-10-18 补：候选图与选中图必须落盘 ★★
    #
    # 这是 AGENTS.md 里记过的同一个坑：「新建的文件状态必须同时加进
    # save_project 与 load_project 的白名单，否则重启后消失」。
    # 当时漏了 candidates / selected 这两个，后果是：
    #   出图成功、文件确实在 candidates/ 下，但 meta.json 里没有记录
    #   → autopilot_due() 判 "没有可用图片" → 5 秒后自动建模被跳过 ✗
    #   → 用户看到图了，但模型永远不自动出来（宇凯报的"对话之后没有生成建模"）
    if state.get("candidates") is not None:
        meta["candidates"] = state.get("candidates") or []
    if state.get("selected"):
        meta["selected"] = state.get("selected")
    if state.get("selected_idx") is not None:
        meta["selected_idx"] = state.get("selected_idx")
    _write_meta(folder, meta)

    # 保存对话历史
    _save_conversation(folder, state.get("conversation", []))


def _load_conversation(folder: Path) -> list:
    try:
        with open(folder / "conversation.json", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _save_conversation(folder: Path, convos: list):
    with open(folder / "conversation.json", "w", encoding="utf-8") as f:
        json.dump(convos, f, ensure_ascii=False, indent=2)


def _list_candidates(folder: Path) -> list[str]:
    """返回候选图路径列表"""
    cand_dir = folder / "candidates"
    if not cand_dir.exists():
        return []
    return sorted([str(p) for p in cand_dir.iterdir() if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp")])


def _get_selected(folder: Path) -> str:
    sel = folder / "selected.jpg"
    return str(sel) if sel.exists() and sel.stat().st_size > 0 else ""


def _get_file(folder: Path, ext: str) -> str:
    f = folder / f"model.{ext}"
    return str(f) if f.exists() and f.stat().st_size > 0 else ""


def save_candidate_image(folder: Path, image_data: bytes, idx: int) -> str:
    """保存候选图，返回路径"""
    cand_dir = folder / "candidates"
    cand_dir.mkdir(exist_ok=True)
    path = cand_dir / f"cand_{idx}.jpg"
    path.write_bytes(image_data)
    return str(path)


def save_selected_image(folder: Path, image_data: bytes) -> str:
    path = folder / "selected.jpg"
    path.write_bytes(image_data)
    return str(path)


def save_glb(folder: Path, data: bytes) -> str:
    path = folder / "model.glb"
    path.write_bytes(data)
    return str(path)


def save_stl(folder: Path, data: bytes) -> str:
    path = folder / "model.stl"
    path.write_bytes(data)
    return str(path)


def archive_project(folder_name: str):
    """将项目标记为已完成（归档）"""
    folder = PROJECTS_DIR / folder_name
    meta = _read_meta(folder)
    meta["archived"] = True
    _write_meta(folder, meta)
