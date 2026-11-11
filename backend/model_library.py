# -*- coding: utf-8 -*-
"""模型库：发布/管理/浏览本地 3D 模型（STL/GLB）"""
from __future__ import annotations
import json, sqlite3, time, hashlib
from contextlib import contextmanager
from pathlib import Path
from datetime import datetime

LIB_DB = Path(__file__).parent.parent / "outputs" / "model_library.db"
LIB_DIR = Path(__file__).parent.parent / "outputs" / "model_library"

# ─── Schema ────────────────────────────────────────────
LIB_DIR.mkdir(parents=True, exist_ok=True)

@contextmanager
def _get_db():
    """一个调用一个连接，用完**关掉**。

    ★ 原来直接 `return con` 给 `with` 用 —— sqlite3 的上下文管理器只 commit/rollback，
      **不 close**。Gradio 是长期进程，每个请求漏一个连接，久了就是句柄泄漏；
      Windows 上临时目录/删除文件还会被占住（测试里当场踩到 PermissionError 32）。
    """
    con = sqlite3.connect(str(LIB_DB), timeout=10)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()

def init_db():
    with _get_db() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS models (
                id          TEXT PRIMARY KEY,
                name        TEXT NOT NULL,
                prompt      TEXT,
                description TEXT,
                file_path   TEXT NOT NULL,
                file_type   TEXT NOT NULL,
                file_size   INTEGER,
                thumb_path  TEXT,
                tags        TEXT,
                created_at  REAL,
                view_count  INTEGER DEFAULT 0,
                dl_count    INTEGER DEFAULT 0
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS tags (
                model_id TEXT PRIMARY KEY,
                tags     TEXT
            )
        """)

init_db()

# ─── Publish ────────────────────────────────────────────
def publish(
    file_path: str,
    name: str = "",
    prompt: str = "",
    description: str = "",
    tags: str = "",
) -> dict:
    """把 STL/GLB 文件发布到本地模型库"""
    p = Path(file_path)
    if not p.exists():
        return {"ok": False, "error": f"文件不存在: {file_path}"}

    fid = hashlib.md5(f"{p.name}{time.time()}".encode()).hexdigest()[:12]
    ext = p.suffix.lower()
    file_type = ext.lstrip(".")

    # 复制到 model_library/
    dest = LIB_DIR / f"{fid}{ext}"
    import shutil
    shutil.copy2(p, dest)

    row = {
        "id": fid,
        "name": name or p.stem,
        "prompt": prompt,
        "description": description,
        "file_path": str(dest),
        "file_type": file_type,
        "file_size": dest.stat().st_size,
        "thumb_path": "",
        "tags": tags,
        "created_at": time.time(),
    }

    with _get_db() as db:
        db.execute(
            "INSERT INTO models (id,name,prompt,description,file_path,file_type,file_size,thumb_path,tags,created_at) "
            "VALUES (:id,:name,:prompt,:description,:file_path,:file_type,:file_size,:thumb_path,:tags,:created_at)",
            row
        )
        db.commit()

    return {"ok": True, "id": fid, "name": row["name"], "file_path": str(dest)}

# ─── Browse ─────────────────────────────────────────────
def list_models(limit: int = 50, offset: int = 0) -> list:
    with _get_db() as db:
        rows = db.execute(
            "SELECT * FROM models ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (limit, offset)
        ).fetchall()
        return [dict(r) for r in rows]

def get_model(model_id: str) -> dict | None:
    with _get_db() as db:
        r = db.execute("SELECT * FROM models WHERE id=?", (model_id,)).fetchone()
        return dict(r) if r else None

def increment_views(model_id: str):
    with _get_db() as db:
        db.execute("UPDATE models SET view_count=view_count+1 WHERE id=?", (model_id,))
        db.commit()

def increment_dls(model_id: str):
    with _get_db() as db:
        db.execute("UPDATE models SET dl_count=dl_count+1 WHERE id=?", (model_id,))
        db.commit()

def delete_model(model_id: str) -> bool:
    m = get_model(model_id)
    if not m:
        return False
    Path(m["file_path"]).unlink(missing_ok=True)
    with _get_db() as db:
        db.execute("DELETE FROM models WHERE id=?", (model_id,))
        db.commit()
    return True

def get_stats() -> dict:
    with _get_db() as db:
        r = db.execute(
            "SELECT COUNT(*) as count, SUM(file_size) as total_size, SUM(view_count) as views, SUM(dl_count) as dls "
            "FROM models"
        ).fetchone()
        return dict(r) if r else {"count": 0, "total_size": 0, "views": 0, "dls": 0}

def render_library_html(models: list, stats: dict = None) -> str:
    if not stats:
        stats = get_stats()

    model_cards = ""
    for m in models:
        size_kb = m["file_size"] // 1024 if m["file_size"] else 0
        created = datetime.fromtimestamp(m["created_at"]).strftime("%Y-%m-%d") if m.get("created_at") else ""
        icon = {"stl": "📦", "glb": "🧊", "gltf": "🧊"}.get(m.get("file_type", "glb"), "📦")
        tags_html = ""
        if m.get("tags"):
            for t in m["tags"].split(","):
                tags_html += f'<span class="tag">{t.strip()}</span>'
        model_cards += f"""
        <div class="card">
            <div class="card-icon">{icon}</div>
            <div class="card-title">{m['name']}</div>
            <div class="card-meta">{created} · {size_kb}KB · 👁{m.get('view_count',0)} · ⬇{m.get('dl_count',0)}</div>
            {('<div class="card-tags">' + tags_html + '</div>') if tags_html else ''}
            <div class="card-actions">
                <a href="/model-library/download/{m['id']}" class="btn">⬇ 下载</a>
                <a href="/model-library/preview/{m['id']}" class="btn btn-secondary">👁 预览</a>
            </div>
        </div>"""

    return f"""
<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>AI 创意工坊 · 模型库</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ background: #1a1a2e; color: #e2e2e8; font-family: system-ui, sans-serif; padding: 24px; }}
  h1 {{ color: #7ec8e3; margin-bottom: 6px; }}
  .subtitle {{ color: #888; margin-bottom: 20px; font-size: 14px; }}
  .stats {{ display: flex; gap: 16px; margin-bottom: 24px; }}
  .stat {{ background: #16213e; padding: 12px 18px; border-radius: 8px; }}
  .stat-num {{ font-size: 22px; font-weight: bold; color: #7ec8e3; }}
  .stat-label {{ font-size: 12px; color: #888; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); gap: 16px; }}
  .card {{ background: #16213e; border-radius: 10px; padding: 16px; border: 1px solid #2a2a4a; }}
  .card-icon {{ font-size: 28px; margin-bottom: 8px; }}
  .card-title {{ font-size: 15px; font-weight: 600; color: #e2e2e8; margin-bottom: 6px; }}
  .card-meta {{ font-size: 12px; color: #777; margin-bottom: 10px; }}
  .card-tags {{ display: flex; flex-wrap: wrap; gap: 4px; margin-bottom: 10px; }}
  .tag {{ background: #1f3460; color: #7ec8e3; font-size: 11px; padding: 2px 8px; border-radius: 99px; }}
  .card-actions {{ display: flex; gap: 8px; }}
  .btn {{ background: #1f3460; color: #7ec8e3; padding: 6px 12px; border-radius: 6px; text-decoration: none; font-size: 13px; }}
  .btn-secondary {{ background: #0f3460; color: #aaa; }}
  .empty {{ color: #555; text-align: center; padding: 60px; }}
  .back {{ color: #7ec8e3; text-decoration: none; font-size: 14px; }}
</style>
</head>
<body>
<a href="/" class="back">← 返回创意工坊</a>
<h1>📦 模型库</h1>
<p class="subtitle">本地私有模型库 · 隐私零泄露</p>
<div class="stats">
  <div class="stat"><div class="stat-num">{stats['count']}</div><div class="stat-label">模型总数</div></div>
  <div class="stat"><div class="stat-num">{stats['total_size']//1024//1024 if stats.get('total_size') else 0}MB</div><div class="stat-label">占用空间</div></div>
  <div class="stat"><div class="stat-num">{stats.get('views', 0)}</div><div class="stat-label">总浏览</div></div>
  <div class="stat"><div class="stat-num">{stats.get('dls', 0)}</div><div class="stat-label">总下载</div></div>
</div>
<div class="grid">
  {model_cards if model_cards else '<div class="empty">还没有发布任何模型<br>在主界面生成 STL 后点击「📤 发布到模型库」</div>'}
</div>
</body>
</html>"""

def render_preview_html(model: dict) -> str:
    file_type = model.get("file_type", "glb")
    icon = {"stl": "📦", "glb": "🧊", "gltf": "🧊"}.get(file_type, "📦")
    size_kb = (model.get("file_size") or 0) // 1024
    created = datetime.fromtimestamp(model["created_at"]).strftime("%Y-%m-%d %H:%M") if model.get("created_at") else ""
    file_url = f"/model-library/file/{model['id']}"
    viewer_js = f"""
    <model-viewer src="{file_url}" 
        {'style="width:100%;height:500px;background:#16213e;border-radius:10px"' if file_type in ("glb","gltf") else 'style="display:none"'}
        auto-rotate camera-controls shadow-intensity="1">
    </model-viewer>
    """ if file_type in ("glb","gltf") else f'<div style="padding:40px;text-align:center;color:#888">STL 格式暂不支持网页 3D 预览<br><a href="{file_url}" class="btn">⬇ 下载后在软件中查看</a></div>'
    model_viewer_cdn = "https://unpkg.com/@google/model-viewer@3.5.0/dist/model-viewer.min.js"
    return f"""
<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>{model['name']} - 模型预览</title>
<script type="module" src="https://unpkg.com/@google/model-viewer@3.5.0/dist/model-viewer.min.js"></script>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ background: #1a1a2e; color: #e2e2e8; font-family: system-ui, sans-serif; padding: 24px; }}
  a {{ color: #7ec8e3; text-decoration: none; }}
  .back {{ font-size: 14px; margin-bottom: 16px; display: block; }}
  .header {{ display: flex; align-items: center; gap: 16px; margin-bottom: 20px; }}
  .icon {{ font-size: 48px; }}
  .title {{ font-size: 24px; font-weight: bold; }}
  .meta {{ color: #888; font-size: 14px; margin-top: 4px; }}
  .viewer {{ margin: 20px 0; border-radius: 10px; overflow: hidden; background: #16213e; }}
  .info {{ background: #16213e; border-radius: 10px; padding: 16px; margin-top: 16px; }}
  .info-row {{ display: flex; gap: 16px; margin-bottom: 8px; font-size: 14px; }}
  .info-label {{ color: #888; width: 80px; }}
  .info-value {{ color: #e2e2e8; }}
  .btn {{ background: #1f3460; color: #7ec8e3; padding: 10px 20px; border-radius: 8px; text-decoration: none; display: inline-block; margin-top: 16px; }}
</style>
</head>
<body>
<a href="/model-library" class="back">← 返回模型库</a>
<div class="header">
  <div class="icon">{icon}</div>
  <div>
    <div class="title">{model['name']}</div>
    <div class="meta">{created} · {size_kb}KB · {file_type.upper()}</div>
  </div>
</div>
<div class="viewer">
  <model-viewer src="{file_url}" 
      {'style="width:100%;height:500px;background:#16213e;border-radius:10px"' if file_type in ("glb","gltf") else 'style="display:none"'}
      auto-rotate camera-controls shadow-intensity="1">
  </model-viewer>
  {f'<div style="padding:40px;text-align:center;color:#888">STL 格式暂不支持网页 3D 预览<br><a href="{file_url}" class="btn">⬇ 下载后在软件中查看</a></div>' if file_type not in ("glb","gltf") else ''}
</div>
<div class="info">
  <div class="info-row"><span class="info-label">描述</span><span class="info-value">{model.get('description') or '（无）'}</span></div>
  <div class="info-row"><span class="info-label">提示词</span><span class="info-value">{model.get('prompt') or '（无）'}</span></div>
</div>
<a href="/model-library/download/{model['id']}" class="btn">⬇ 下载 STL/GLB</a>
</body>
</html>"""
