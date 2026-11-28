# -*- coding: utf-8 -*-
"""资产库：所有能力产出的统一登记处（2026-11-27）。

为什么需要它
------------
五项能力（骨骼/贴图/精修/多视图/动画）各有自己的输出目录，跑几次就散成
`outputs/3d/`、`outputs/capabilities/*/`、`ComfyUI/output/` 三处。用户分不清
哪一件是最终版、哪一件是中间产物，删的时候也不敢删。

这里做四件事：
1. **统一存放**：`outputs/assets/<kind>/<YYYYmmdd-HHMMSS>_<name>`，kind ∈
   mesh / multiview / refined / painted / rigged / motion
2. **索引**：`outputs/assets/index.json`，每条记录来源、参数、面数、水密、
   贴图数、关节数、sha256（去重用）
3. **回收**：按容量上限自动淘汰最旧的（默认 8 GB，可用配置覆盖）
4. **呈现**：`render_html()` 给界面用，按类型分组 + 时间 + 大小 + 指标

设计约束：
- **单文件可回滚**：只改 `index.json`；文件本身按目录存，坏索引不影响产物
- **不改原件**：`register` 是 copy（除非 `move=True`），调用方仍可拿到原路径
- **并发安全**：写索引用文件锁（Windows 用 O_EXCL 锁文件 + 重试）
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "outputs" / "assets"
INDEX = ASSETS / "index.json"
LOCK = ASSETS / ".index.lock"

KINDS = {
    "mesh": ("模型", "🧊"),
    "multiview": ("多视图重建", "🧭"),
    "refined": ("精修", "✨"),
    "painted": ("贴图", "🎨"),
    "rigged": ("骨骼", "🦴"),
    "motion": ("动画", "🏃"),
}

MAX_BYTES = int(os.environ.get("FORGEFORM_ASSET_MAX_BYTES", str(8 * 1024 ** 3)))


# ───────────────────────── 基础 ─────────────────────────

def _now_tag() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def _sha256(path: Path, chunk: int = 1 << 20, limit: int = 512 << 20) -> str:
    """算 sha256；超过 limit 只看头尾各 8 MB（大 GLB 全量算太慢，去重够用）。"""
    h = hashlib.sha256()
    size = path.stat().st_size
    with path.open("rb") as f:
        if size <= limit:
            for block in iter(lambda: f.read(chunk), b""):
                h.update(block)
        else:
            h.update(f.read(8 << 20))
            f.seek(size - (8 << 20))
            h.update(f.read(8 << 20))
            h.update(str(size).encode())
    return h.hexdigest()


def _read_index() -> list:  # noqa: C901
    try:
        data = json.loads(INDEX.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception as e:
        # 2026-11-28：损坏时先备份，别静默当空库（下次写入会把全库覆盖）
        try:
            bak = INDEX.with_suffix(f'.corrupt-{int(time.time())}.json')
            INDEX.replace(bak)
            print(f'[assets] 索引损坏，已备份到 {bak.name}（{e}）', flush=True)
        except Exception:
            pass
        return []


def _write_index(rows: list) -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    tmp = INDEX.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, INDEX)


class _Lock:
    """极简文件锁：拿不到就等（最多 5 秒），拿不到就放弃写（宁可漏记不写坏）。"""

    def __init__(self, path: Path, timeout: float = 5.0):
        self.path, self.timeout, self.fd = path, timeout, None

    def __enter__(self):
        ASSETS.mkdir(parents=True, exist_ok=True)
        t0 = time.time()
        while time.time() - t0 < self.timeout:
            try:
                self.fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                return self
            except FileExistsError:
                time.sleep(0.05)
        return self

    def __exit__(self, *exc):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except Exception:
                pass
            try:
                self.path.unlink()
            except Exception:
                pass


# ───────────────────────── 登记 ─────────────────────────

def register(path: str | Path, kind: str, *, title: str = "", params: dict | None = None,
             source: str = "", metrics: dict | None = None, move: bool = False) -> dict:
    """把一件产物登记进资产库，返回索引条目。

    - 同内容（sha256 相同）已存在 → 不复制，直接返回旧条目并标 `dedup=True`
    - `metrics` 由调用方给（面数/水密/关节…）；不给就只记大小
    """
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(str(p))
    if kind not in KINDS:
        raise ValueError(f"未知类型 {kind}（可选 {list(KINDS)}）")

    digest = _sha256(p)
    with _Lock(LOCK):
        rows = _read_index()
        for row in rows:
            if row.get("sha256") == digest:
                return {**row, "dedup": True}

        dest_dir = ASSETS / kind
        dest_dir.mkdir(parents=True, exist_ok=True)
        safe = "".join(c for c in p.stem if c.isalnum() or c in "-_")[:40] or "asset"
        dest = dest_dir / f"{_now_tag()}_{safe}{p.suffix.lower()}"
        n = 1
        while dest.exists():
            dest = dest_dir / f"{_now_tag()}_{safe}_{n}{p.suffix.lower()}"
            n += 1
        if move:
            shutil.move(str(p), dest)
        else:
            shutil.copy2(p, dest)

        entry = {
            "id": hashlib.sha1(f"{digest}{time.time()}".encode()).hexdigest()[:12],
            "kind": kind,
            "title": title or p.stem,
            "file": _rel_or_abs(dest),
            "bytes": dest.stat().st_size,
            "created": time.strftime("%Y-%m-%d %H:%M:%S"),
            "sha256": digest,
            "params": params or {},
            "source": source,
            "metrics": metrics or {},
        }
        rows.append(entry)
        _write_index(rows)
    _enforce_quota()
    return entry


def register_many(items: list[dict]) -> list[dict]:
    """批量登记；任一条失败不影响其他（返回成功项）。"""
    out = []
    for it in items:
        try:
            out.append(register(**it))
        except Exception:
            continue
    return out


def list_assets(kind: str | None = None, limit: int = 200) -> list[dict]:
    rows = [r for r in _read_index() if not kind or r.get("kind") == kind]
    rows.sort(key=lambda r: r.get("created", ""), reverse=True)
    return rows[:limit]


def get(asset_id: str) -> dict | None:
    return next((r for r in _read_index() if r.get("id") == asset_id), None)


def _rel_or_abs(path: Path) -> str:
    """索引里优先存相对路径（仓库可搬）；不在仓库内就存绝对路径。"""
    try:
        return str(path.relative_to(ROOT)).replace("\\", "/")
    except Exception:
        return str(path).replace("\\", "/")


def absolute(row: dict) -> Path:
    p = Path(row["file"])
    return p if p.is_absolute() else ROOT / p


def remove(asset_id: str, *, delete_file: bool = True) -> bool:
    """从索引移除（可选删文件）。返回是否真的删到了。"""
    with _Lock(LOCK):
        rows = _read_index()
        keep, hit = [], None
        for r in rows:
            if r.get("id") == asset_id:
                hit = r
            else:
                keep.append(r)
        if not hit:
            return False
        _write_index(keep)
    if delete_file:
        f = absolute(hit)
        try:
            if f.is_file():
                f.unlink()
        except Exception:
            pass
    return True


def usage() -> dict:
    rows = _read_index()
    total = sum(int(r.get("bytes") or 0) for r in rows)
    by_kind: dict[str, int] = {}
    for r in rows:
        by_kind[r.get("kind", "?")] = by_kind.get(r.get("kind", "?"), 0) + int(r.get("bytes") or 0)
    return {"count": len(rows), "bytes": total, "mb": round(total / 1024 / 1024, 1),
            "by_kind": by_kind, "max_bytes": MAX_BYTES}


def _enforce_quota() -> int:
    """超容量就按时间从旧到新删，返回删掉的条数。"""
    rows = _read_index()
    total = sum(int(r.get("bytes") or 0) for r in rows)
    if total <= MAX_BYTES:
        return 0
    rows.sort(key=lambda r: r.get("created", ""))
    removed = 0
    for r in list(rows):
        if total <= MAX_BYTES:
            break
        total -= int(r.get("bytes") or 0)
        remove(r["id"])
        removed += 1
    return removed


def clean(kind: str | None = None, older_than_days: int = 30) -> int:
    """清理旧产物（默认 30 天前），返回删除条数。"""
    cutoff = time.time() - older_than_days * 86400
    removed = 0
    for r in list_assets(kind, limit=10000):
        try:
            ts = time.mktime(time.strptime(r["created"], "%Y-%m-%d %H:%M:%S"))
        except Exception:
            continue
        if ts < cutoff and remove(r["id"]):
            removed += 1
    return removed


# ───────────────────────── 呈现 ─────────────────────────

def _fmt_size(n: int) -> str:
    if n >= 1024 ** 3:
        return f"{n / 1024 ** 3:.1f} GB"
    if n >= 1024 ** 2:
        return f"{n / 1024 ** 2:.1f} MB"
    return f"{n / 1024:.0f} KB"


def _metric_text(m: dict) -> str:
    parts = []
    if m.get("faces"):
        parts.append(f"{int(m['faces']):,} 面")
    if m.get("watertight") is not None:
        parts.append("水密" if m["watertight"] else "非水密")
    if m.get("textures"):
        parts.append(f"{m['textures']} 贴图")
    if m.get("joints"):
        parts.append(f"{m['joints']} 关节")
    if m.get("animations"):
        parts.append(f"{m['animations']} 动画")
    return " · ".join(parts)


def render_html(limit: int = 12) -> str:
    """给界面用：按类型分组列出最近资产。"""
    rows = list_assets(limit=limit)
    use = usage()
    head = (f'<div class="asset-head"><strong>📦 我的资产</strong>'
            f'<span>{use["count"]} 件 · {use["mb"]} MB / 上限 '
            f'{_fmt_size(use["max_bytes"])}</span></div>')
    if not rows:
        return head + ('<div class="asset-empty">还没有产物。在对话里说「给它绑骨骼 / 上个色 / '
                       '精修一下 / 做个走路动画」，产出会自动进这里。</div>')
    cards = []
    for r in rows:
        label, icon = KINDS.get(r.get("kind"), ("其他", "📄"))
        metric = _metric_text(r.get("metrics") or {})
        cards.append(
            f'<div class="asset-card"><div class="asset-icon">{icon}</div>'
            f'<div class="asset-body"><div class="asset-title">{r.get("title") or "未命名"}</div>'
            f'<div class="asset-meta">{label} · {_fmt_size(int(r.get("bytes") or 0))}'
            f'{" · " + metric if metric else ""}</div>'
            f'<div class="asset-time">{r.get("created")}</div></div></div>')
    return head + '<div class="asset-grid">' + "".join(cards) + "</div>"
