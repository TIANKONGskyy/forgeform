# -*- coding: utf-8 -*-
"""云端 API 接入：扩写（文本）、出图、建模。三家可以是不同厂商。

## 设计取向

云厂商的接口形状各不相同，但**九成都能归到两种模式**：

    sync    一次 POST 就把结果拿回来（体里带 base64/URL）
    async   先 POST 拿任务 id，再 GET 轮询到 done，最后拿产物 URL

所以这里不写"每个厂商一个类"，而是写**一个通用适配器 + 配置**：
`config.json → cloud.<用途>` 里写清 URL 模板、字段名、完成态取值。
真要接新厂商，改配置就行，不用改代码。

## 用途与配置

    cloud.enhancer   提示词扩写（OpenAI /v1/chat/completions 兼容）
    cloud.image      出图（OpenAI /v1/images/generations 兼容，或自定义 sync）
    cloud.mesh       图生 3D（多为 async 任务制）

每个用途形如：

    {
      "enabled": true,
      "provider": "openai",              # 仅用于展示
      "base_url": "https://api.openai.com/v1",
      "model": "gpt-4o-mini",
      "key_name": "openai",              # backend.secrets 里的名字
      "mode": "sync",                    # 或 "async"
      "timeout": 180,
      "poll": {"url": "{base}/tasks/{id}", "interval": 3, "done": ["SUCCEEDED"],
               "fail": ["FAILED", "CANCELED"], "status_field": "status",
               "result_field": "model_urls.glb"},
      "submit": {"url": "{base}/openapi/v1/tasks", "body": {...}}
    }

`{base}` / `{id}` / `{key}` 会被替换；`body` 里的 `{image_b64}` `{prompt}`
也会被替换 —— 这样同一个适配器能覆盖 Meshy / Tripo / Rodin 那几种常见的
"提交 → 轮询 → 下载"流程。

## 密钥

key 从 `backend.secrets` 取（DPAPI 加密，绑定本机本用户）。**不读环境变量**
—— 环境变量会被子进程继承，等于把 key 递给每一个被调起的程序。
"""
from __future__ import annotations

import base64
import json
import mimetypes
import os
import time
import urllib.error
import urllib.request
from pathlib import Path


class CloudError(RuntimeError):
    pass


# ── 配置读取 ──────────────────────────────────────────────────
def _cfg() -> dict:
    root = Path(__file__).resolve().parent.parent
    try:
        return json.loads((root / "config.json").read_text(encoding="utf-8-sig"))
    except Exception:
        return {}


def use_cfg(use: str) -> dict:
    """取某一用途的云配置（enhancer / image / mesh）。没有就返回 {}。"""
    cloud = (_cfg().get("cloud") or {})
    if not cloud.get("enabled", True):
        return {}
    c = cloud.get(use) or {}
    if not c.get("enabled", True):
        return {}
    if not c.get("base_url"):
        return {}
    return c


def available(use: str) -> bool:
    """配了 && 有 key 才算能用（避免配了地址没 key 时反复撞 401）。"""
    c = use_cfg(use)
    if not c:
        return False
    from backend.secrets import get_secret
    return bool(get_secret(c.get("key_name") or use))


def _key(c: dict) -> str:
    from backend.secrets import get_secret
    name = c.get("key_name") or ""
    k = get_secret(name) if name else ""
    if not k:
        raise CloudError(f"云端 {name or '?'} 没有可用密钥 —— 在「模型与配置 → 云端 API」里填一次")
    return k


def _expand(tpl: str, base: str, **kw) -> str:
    """支持 {base} {id} {prompt} {image_b64} 这类占位符。"""
    out = str(tpl or "")
    out = out.replace("{base}", base.rstrip("/"))
    for k, v in kw.items():
        out = out.replace("{" + k + "}", str(v))
    return out


def _request(url: str, data: bytes | None, headers: dict, timeout: int,
             method: str = "POST"):
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "ignore")[:400]
        except Exception:
            pass
        raise CloudError(f"HTTP {e.code} {url}: {body}") from e
    except urllib.error.URLError as e:
        raise CloudError(f"连不上 {url}: {e.reason}") from e


def _json_or_text(raw: bytes):
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return raw.decode("utf-8", "ignore")


def _dig(obj, dotted: str, default=None):
    """按 'a.b.c' 取值，兼容列表下标（'urls.0'）。"""
    cur = obj
    for part in str(dotted or "").split("."):
        if not part:
            continue
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
                continue
            except (ValueError, IndexError):
                return default
        if isinstance(cur, dict):
            if part not in cur:
                return default
            cur = cur[part]
        else:
            return default
    return cur


def _download(url: str, timeout: int = 300) -> bytes:
    raw, _ = _request(url, None, {"User-Agent": "ForgeForm/1.0"}, timeout, "GET")
    return raw


# ── 产物校验：看魔数，不看大小 ──────────────────────────────────
# 云端偶尔会把错误页（HTML/JSON）当成图片/模型返回，HTTP 状态还是 200。
# 用文件大小当判据不可靠 —— 纯色 PNG 可以只有几百字节，而错误页可能几 KB。
_IMAGE_MAGIC = (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"RIFF", b"GIF8", b"BM")
_MESH_MAGIC = (b"glTF", b"ply\n", b"solid ", b"OBJ", b"# ")


def _check_image(path: Path) -> None:
    head = path.read_bytes()[:16]
    if not any(head.startswith(m) for m in _IMAGE_MAGIC):
        raise CloudError(f"出的图不是图片（开头 {head[:12]!r}），多半是错误页或 HTML")


def _check_mesh(path: Path) -> None:
    raw = path.read_bytes()[:64]
    if any(raw.startswith(m) for m in _MESH_MAGIC):
        return
    # ASCII STL / OBJ 允许前导空白与注释
    text = raw.lstrip()
    if text[:5].lower() == b"solid" or text[:2] == b"v ":
        return
    raise CloudError(f"建模产物不是网格（开头 {raw[:12]!r}），多半是错误页或 JSON 错误体")


def _looks_like_error_page(path: Path) -> bool:
    head = path.read_bytes()[:200].lstrip().lower()
    return head.startswith((b"<!doctype", b"<html", b"{", b"["))


# ══════════════════════════════════════════════════════════════════
# 扩写（文本）
# ══════════════════════════════════════════════════════════════════
def chat(prompt: str, timeout: int | None = None, json_mode: bool = False,
         cfg: dict | None = None) -> str:
    """OpenAI 兼容的 /chat/completions。返回正文（不含 reasoning）。"""
    c = cfg if cfg else use_cfg("enhancer")
    if not c:
        raise CloudError("云端扩写没有配置")
    base = str(c.get("base_url") or "").rstrip("/")
    url = _expand(c.get("chat_url") or "{base}/chat/completions", base)
    body = {
        "model": c.get("model") or "gpt-4o-mini",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": float(c.get("temperature", 0.4)),
    }
    if c.get("max_tokens"):
        body["max_tokens"] = int(c["max_tokens"])
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    raw, _ = _request(
        url, json.dumps(body).encode(),
        {"Content-Type": "application/json",
         "Authorization": "Bearer " + _key(c)},
        int(timeout or c.get("timeout") or 120))
    d = _json_or_text(raw)
    if not isinstance(d, dict):
        raise CloudError(f"云端扩写返回非 JSON: {str(d)[:200]}")
    msg = ((d.get("choices") or [{}])[0].get("message") or {})
    text = (msg.get("content") or "").strip()
    if not text:
        raise CloudError("云端扩写返回正文为空")
    return text


# ══════════════════════════════════════════════════════════════════
# 出图
# ══════════════════════════════════════════════════════════════════
def image(prompt: str, out_path: str, size: str = "1024x1024",
          timeout: int | None = None, cfg: dict | None = None) -> str:
    """出图并写成文件。支持 OpenAI 形状（b64_json / url），也支持自定义 sync。"""
    c = cfg if cfg else use_cfg("image")
    if not c:
        raise CloudError("云端出图没有配置")
    base = str(c.get("base_url") or "").rstrip("/")
    mode = (c.get("mode") or "sync").lower()
    url = _expand(c.get("submit_url") or "{base}/images/generations", base)
    body = {
        "model": c.get("model") or "gpt-image-1",
        "prompt": prompt,
        "size": size,
        "n": 1,
    }
    body.update(c.get("extra_body") or {})
    raw, _ = _request(
        url, json.dumps(body).encode(),
        {"Content-Type": "application/json",
         "Authorization": "Bearer " + _key(c)},
        int(timeout or c.get("timeout") or 180))
    d = _json_or_text(raw)
    if not isinstance(d, dict):
        raise CloudError(f"出图返回非 JSON: {str(d)[:200]}")

    if mode == "async":
        d = _poll(c, d)
        glb_url = _dig(d, c.get("image_field") or "output.0.url")
        if not glb_url:
            raise CloudError(f"异步出图完成但找不到图片地址: {json.dumps(d)[:200]}")
        data = _download(str(glb_url))
    else:
        b64 = _dig(d, (c.get("image_b64_field") or "data.0.b64_json"))
        if b64:
            data = base64.b64decode(b64)
        else:
            u = _dig(d, c.get("image_url_field") or "data.0.url")
            if not u:
                raise CloudError(f"出图返回里没有 b64_json 也没有 url: {json.dumps(d)[:200]}")
            data = _download(str(u))

    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    _check_image(p)
    return str(p)


# ══════════════════════════════════════════════════════════════════
# 图生 3D
# ══════════════════════════════════════════════════════════════════
def _poll(c: dict, submitted: dict) -> dict:
    """async 模式的通用轮询。"""
    poll = c.get("poll") or {}
    base = str(c.get("base_url") or "").rstrip("/")
    task_id = _dig(submitted, poll.get("id_field") or "id") \
        or _dig(submitted, "result.id") or _dig(submitted, "task_id")
    if not task_id:
        raise CloudError(f"提交成功但拿不到任务 id: {json.dumps(submitted)[:200]}")
    url_tpl = poll.get("url") or "{base}/tasks/{id}"
    interval = float(poll.get("interval", 3))
    deadline = time.time() + float(poll.get("max_wait", 900))
    done = [str(x).upper() for x in (poll.get("done") or ["SUCCEEDED", "SUCCESS", "COMPLETED"])]
    fail = [str(x).upper() for x in (poll.get("fail") or ["FAILED", "ERROR", "CANCELED"])]
    status_field = poll.get("status_field") or "status"

    while time.time() < deadline:
        raw, _ = _request(_expand(url_tpl, base, id=task_id), None,
                          {"Authorization": "Bearer " + _key(c)}, 60, "GET")
        d = _json_or_text(raw)
        if not isinstance(d, dict):
            time.sleep(interval)
            continue
        st = str(_dig(d, status_field) or "").upper()
        if st in done:
            return d
        if st in fail:
            raise CloudError(f"云端任务失败（{st}）: {json.dumps(d)[:250]}")
        time.sleep(interval)
    raise CloudError(f"云端任务超时（{poll.get('max_wait', 900)} 秒）")


def mesh(image_path: str, out_path: str, timeout: int | None = None,
         cfg: dict | None = None) -> str:
    """图生 3D，产物写成 GLB/OBJ 文件。"""
    c = cfg if cfg else use_cfg("mesh")
    if not c:
        raise CloudError("云端建模没有配置")
    base = str(c.get("base_url") or "").rstrip("/")
    mode = (c.get("mode") or "async").lower()

    src = Path(image_path)
    if not src.is_file():
        raise CloudError(f"输入图不存在: {image_path}")
    img_b64 = base64.b64encode(src.read_bytes()).decode()
    mime = mimetypes.guess_type(src.name)[0] or "image/png"

    submit_tpl = c.get("submit_url") or "{base}/openapi/v1/tasks"
    url = _expand(submit_tpl, base)
    body_tpl = c.get("submit") or {}
    body = json.loads(json.dumps(body_tpl.get("body") or {})) if body_tpl.get("body") else {}
    # 递归替换占位符（{image_b64} {image_mime} {prompt}）
    def walk(node):
        if isinstance(node, dict):
            return {k: walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(v) for v in node]
        if isinstance(node, str):
            return (node.replace("{image_b64}", img_b64)
                        .replace("{image_mime}", mime)
                        .replace("{image_url}", str(src)))
        return node
    body = walk(body) if body else {"image_base64": img_b64, "image_mime": mime}

    raw, _ = _request(
        url, json.dumps(body).encode(),
        {"Content-Type": "application/json",
         "Authorization": "Bearer " + _key(c)},
        int(timeout or c.get("timeout") or 180))
    d = _json_or_text(raw)
    if not isinstance(d, dict):
        raise CloudError(f"建模提交返回非 JSON: {str(d)[:200]}")

    if mode == "async":
        d = _poll(c, d)

    field = c.get("mesh_field") or "model_urls.glb"
    target = _dig(d, field)
    if not target:
        # 有些厂商直接把 GLB base64 塞回体里
        for k in ("glb_base64", "model_base64", "b64"):
            if d.get(k):
                data = base64.b64decode(d[k])
                break
        else:
            raise CloudError(f"建模完成但找不到产物（字段 {field}）: {json.dumps(d)[:250]}")
    else:
        data = _download(str(target))

    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    _check_mesh(p)
    return str(p)


# ══════════════════════════════════════════════════════════════════
# 自检
# ══════════════════════════════════════════════════════════════════
def status() -> dict:
    """三个用途各自的可用性 + 缺什么。UI 和 CLI 都用这个。"""
    from backend.secrets import backend_name, is_weak
    out = {"crypto": backend_name(), "weak_crypto": is_weak(), "uses": {}}
    for use, label in (("enhancer", "提示词扩写"), ("image", "出图"), ("mesh", "建模")):
        c = use_cfg(use)
        configured = bool(c)
        keyed = available(use) if configured else False
        missing = []
        if not configured:
            missing.append("没配 base_url")
        elif not keyed:
            missing.append("没填 key")
        out["uses"][use] = {
            "label": label, "configured": configured, "keyed": keyed,
            "provider": (c.get("provider") or "") if c else "",
            "base_url": (c.get("base_url") or "") if c else "",
            "model": (c.get("model") or "") if c else "",
            "key_name": (c.get("key_name") or use) if c else "",
            "missing": missing,
        }
    return out
