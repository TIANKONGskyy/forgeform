# -*- coding: utf-8 -*-
"""把本机的模型能力开成一个 HTTP API，给别人（或别的程序）调用。

## 为什么单独起一个进程

铸想的 Gradio 界面是给人点的；这个 API 是给程序调的。混在一起会让
"改界面" 和 "改协议" 互相牵连。独立进程还能单独重启、单独限流、单独关掉。

## 对外形状

    自有协议（任务制，适合长任务）
        POST /v1/generate            提交 → 202 {job_id}
        GET  /v1/jobs/{id}           查状态 {status, stage, percent, elapsed}
        GET  /v1/jobs/{id}/files/{k} 取产物（image / glb / stl）

    OpenAI 兼容（适合同事拿现成 SDK 直接调）
        POST /v1/chat/completions    对话（转发到本机 LLM）
        POST /v1/images/generations  出图（b64_json 或 url）
        GET  /v1/models              模型清单

    元信息
        GET  /v1/health              活着吗 + 队列深度
        GET  /openapi.json           OpenAPI 3.1 文档
        GET  /docs                   人看的接口页

## 鉴权

`Authorization: Bearer ff_xxx`。key 由本机签发（`tools/api_key.py`），
**服务端只存 sha256**，明文只在签发那一刻显示一次。每个 key 独立限流，
默认 6 次/分钟、同时 2 个任务。

## 起法

    PYTHONPATH= python tools/api_server.py               # 127.0.0.1:7863
    PYTHONPATH= python tools/api_server.py --port 9000 --host 0.0.0.0
"""
from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import queue
import re
import sys
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend import secrets as S  # noqa: E402

JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()
JOB_QUEUE: "queue.Queue[str]" = queue.Queue()
_RATE: dict[str, list[float]] = {}
_RATE_LOCK = threading.Lock()

# 限流分两档 —— 生成类一分钟几次就够，但**轮询状态**是客户端必须做的动作，
# 用同一个桶会把正常轮询打成 429（实测：4 秒轮询一个 60 秒任务 = 15 次/分钟，
# 直接超 6 次限额，客户端以为接口坏了）。读接口单独给一个宽桶。
RATE_WRITE_PER_MIN = 6        # POST /v1/generate、/chat/completions、/images/generations
RATE_READ_PER_MIN = 240       # GET /v1/jobs/*、/v1/models
MAX_CONCURRENT = 2
JOB_TTL = 6 * 3600          # 6 小时后清掉任务记录（产物文件不删）
_running = {"n": 0}
_running_lock = threading.Lock()

_PIPELINE = None
_PIPE_LOCK = threading.Lock()


# ══════════════════════════════════════════════════════════════════
# 工具
# ══════════════════════════════════════════════════════════════════
def pipeline():
    global _PIPELINE
    with _PIPE_LOCK:
        if _PIPELINE is None:
            from backend.pipeline import Pipeline
            _PIPELINE = Pipeline(str(ROOT / "config.json"))
        return _PIPELINE


def config() -> dict:
    try:
        return json.loads((ROOT / "config.json").read_text(encoding="utf-8-sig"))
    except Exception:
        return {}


def _job_public(j: dict) -> dict:
    return {
        "job_id": j["id"],
        "status": j["status"],
        "stage": j.get("stage") or "",
        "percent": round(float(j.get("percent") or 0), 1),
        "elapsed_sec": round(time.time() - j["created"], 1),
        "prompt": j.get("prompt", ""),
        "error": j.get("error") or "",
        "files": {k: f"/v1/jobs/{j['id']}/files/{k}" for k in (j.get("files") or {})},
        "meta": j.get("meta") or {},
    }


def _new_job(prompt: str, opts: dict) -> dict:
    jid = uuid.uuid4().hex[:16]
    j = {"id": jid, "status": "queued", "stage": "", "percent": 0.0,
         "created": time.time(), "prompt": prompt, "opts": opts,
         "files": {}, "error": "", "meta": {}}
    with JOBS_LOCK:
        JOBS[jid] = j
    return j


def _rate_ok(key_label: str, bucket: str = "write") -> bool:
    limit = RATE_WRITE_PER_MIN if bucket == "write" else RATE_READ_PER_MIN
    slot = f"{bucket}:{key_label}"
    now = time.time()
    with _RATE_LOCK:
        hits = [t for t in _RATE.get(slot, []) if now - t < 60]
        if len(hits) >= limit:
            _RATE[slot] = hits
            return False
        hits.append(now)
        _RATE[slot] = hits
        return True


def _gc() -> None:
    cutoff = time.time() - JOB_TTL
    with JOBS_LOCK:
        for jid in [k for k, v in JOBS.items() if v["created"] < cutoff]:
            JOBS.pop(jid, None)


def _stage_percent(j: dict, stage: str, pct: float) -> None:
    with JOBS_LOCK:
        if j["id"] in JOBS:
            j["stage"] = stage
            j["percent"] = max(j.get("percent") or 0, pct)
            j["status"] = "running"


# ══════════════════════════════════════════════════════════════════
# 任务执行
# ══════════════════════════════════════════════════════════════════
def _run_job(j: dict) -> None:
    opts = j.get("opts") or {}
    stages = opts.get("stages") or ["image", "mesh", "stl"]
    quality = opts.get("quality") or "标准"
    img_model = opts.get("image_model") or ""
    mesh_model = opts.get("model_3d") or ""
    try:
        with _running_lock:
            _running["n"] += 1
        j["status"] = "running"
        pipe = pipeline()
        cfg = pipe.config
        if not img_model:
            img_model = ((cfg.get("stages") or {}).get("image") or {}).get("default_model", "")
        if not mesh_model:
            mesh_model = ((cfg.get("stages") or {}).get("3d") or {}).get("default_model", "")

        img_path = ""
        if "image" in stages:
            _stage_percent(j, "image", 5)
            results = pipe.generate_images(
                j["prompt"], count=1,
                model_key=img_model, quality_name=quality,
                resolution=opts.get("resolution") or "1024×1024",
                skip_cache=True)
            ok = [r for r in (results or []) if getattr(r, "success", False) and r.output_path]
            if not ok:
                raise RuntimeError("出图失败（模型不可用或显存不足）")
            img_path = ok[0].output_path
            j["files"]["image"] = img_path
            _stage_percent(j, "image", 45)
        else:
            img_path = opts.get("image_path") or ""

        glb_path = ""
        if "mesh" in stages:
            if not img_path:
                raise RuntimeError("要建模必须先有图片（stages 里加 image，或传 image_path）")
            _stage_percent(j, "mesh", 50)
            r3 = pipe.generate_3d(model_key=mesh_model, quality_name=quality,
                                  image_path=img_path)
            if not (r3 and getattr(r3, "success", False) and r3.output_path):
                raise RuntimeError("建模失败")
            glb_path = r3.output_path
            j["files"]["glb"] = glb_path
            _stage_percent(j, "mesh", 85)

        if "stl" in stages:
            if not glb_path:
                raise RuntimeError("要导 STL 必须先建模")
            _stage_percent(j, "stl", 88)
            ok, stl_path = pipe.export_stl(glb_path, float(opts.get("size_mm") or 120.0))
            if not ok:
                raise RuntimeError("STL 导出失败")
            j["files"]["stl"] = stl_path

        with JOBS_LOCK:
            j["status"] = "succeeded"
            j["stage"] = "done"
            j["percent"] = 100.0
    except Exception as exc:
        with JOBS_LOCK:
            j["status"] = "failed"
            j["error"] = f"{type(exc).__name__}: {exc}"
        print(f"[api] job {j['id']} 失败: {exc}\n{traceback.format_exc()}", flush=True)
    finally:
        with _running_lock:
            _running["n"] = max(0, _running["n"] - 1)


def _worker() -> None:
    while True:
        jid = JOB_QUEUE.get()
        with JOBS_LOCK:
            j = JOBS.get(jid)
        if not j:
            continue
        # 并发上限：满了就等（队列本来就是排队用的）
        while True:
            with _running_lock:
                if _running["n"] < MAX_CONCURRENT:
                    break
            time.sleep(0.3)
        _run_job(j)


# ══════════════════════════════════════════════════════════════════
# HTTP
# ══════════════════════════════════════════════════════════════════
class Handler(BaseHTTPRequestHandler):
    server_version = "ForgeFormAPI/1.0"
    protocol_version = "HTTP/1.1"

    # ── 基础 ──
    def _audit(self, code: int, note: str = "") -> None:
        """每次响应记一条。key id 从 _auth 存在 self._key_id 上。"""
        try:
            S.audit(getattr(self, "_key_id", "-"), self.command, self.path,
                    code, self.client_address[0] if self.client_address else "",
                    note=note)
        except Exception:
            pass

    def _send(self, obj, code: int = 200, mime: str = "application/json") -> None:
        self._audit(code)
        body = obj if isinstance(obj, bytes) else json.dumps(
            obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", mime + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _problem(self, code: int, slug: str, detail: str) -> None:
        """RFC 9457 problem+json —— 错误体形状统一，客户端不用猜。"""
        self._send({"type": f"https://forgeform.local/err/{slug}",
                    "title": slug, "status": code, "detail": detail},
                   code, "application/problem+json")

    def _auth(self, bucket: str = "write") -> dict | None:
        h = self.headers.get("Authorization", "")
        m = re.match(r"^Bearer\s+(.+)$", h or "", re.I)
        if not m:
            self._problem(401, "unauthorized", "需要 Authorization: Bearer <key>")
            return None
        ip = self.client_address[0] if self.client_address else ""
        rec = S.verify_api_key(m.group(1).strip(), ip=ip)
        if not rec:
            # 审计里只记"被拒"这件事，不记 token 片段（日志也是泄露面）
            S.audit("-", self.command, self.path, 401, ip, note="rejected")
            self._problem(401, "invalid-key", "key 无效、已撤销或已过期")
            return None
        self._key_id = rec.get("id") or "-"
        if not _rate_ok(rec.get("label") or "default", bucket):
            self.send_response(429)
            self.send_header("Retry-After", "10")
            body = json.dumps({"type": "https://forgeform.local/err/rate-limited",
                               "title": "rate-limited", "status": 429,
                               "detail": ("超过 " + str(RATE_WRITE_PER_MIN if bucket == "write" else RATE_READ_PER_MIN) + " 次/分钟")},
                              ensure_ascii=False).encode()
            self.send_header("Content-Type", "application/problem+json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return None
        return rec

    def log_message(self, fmt, *args) -> None:
        print(f"[api] {self.address_string()} {fmt % args}", flush=True)

    # ── GET ──
    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/v1/health":
            return self._send({"ok": True, "queue": JOB_QUEUE.qsize(),
                               "running": _running["n"],
                               "jobs": len(JOBS), "ts": time.time()})
        if path == "/openapi.json":
            return self._send(OPENAPI)
        if path == "/docs":
            return self._send(DOCS_HTML.encode(), mime="text/html")

        if not self._auth("read"):
            return
        if path == "/v1/models":
            return self._send(_models())
        m = re.match(r"^/v1/jobs/([0-9a-f]+)$", path)
        if m:
            with JOBS_LOCK:
                j = JOBS.get(m.group(1))
            if not j:
                return self._problem(404, "job-not-found", "没有这个任务")
            return self._send(_job_public(j))
        m = re.match(r"^/v1/jobs/([0-9a-f]+)/files/([a-z0-9]+)$", path)
        if m:
            with JOBS_LOCK:
                j = JOBS.get(m.group(1))
            if not j:
                return self._problem(404, "job-not-found", "没有这个任务")
            p = (j.get("files") or {}).get(m.group(2))
            if not p or not Path(p).is_file():
                return self._problem(404, "file-not-ready", "这个产物还没有/已清理")
            fp = Path(p)
            mime = mimetypes.guess_type(fp.name)[0] or "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(fp.stat().st_size))
            self.send_header("Content-Disposition",
                             f'attachment; filename="{fp.name}"')
            self.end_headers()
            self.wfile.write(fp.read_bytes())
            self._audit(200, note="file")
            return
        self._problem(404, "not-found", f"没有 {path}")

    # ── POST ──
    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        auth = self._auth()
        if not auth:
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if size > 32 * 1024 * 1024:
                return self._problem(413, "too-large", "请求体超过 32 MB")
            body = json.loads(self.rfile.read(size) or b"{}")
        except Exception:
            return self._problem(400, "bad-json", "请求体不是合法 JSON")
        try:
            if path == "/v1/generate":
                return self._post_generate(body)
            if path == "/v1/chat/completions":
                return self._post_chat(body)
            if path == "/v1/images/generations":
                return self._post_images(body)
        except Exception as exc:
            return self._problem(500, "internal", f"{type(exc).__name__}: {exc}")
        self._problem(404, "not-found", f"没有 {path}")

    def _post_generate(self, body: dict):
        prompt = str(body.get("prompt") or "").strip()
        if not prompt:
            return self._problem(400, "missing-prompt", "prompt 不能为空")
        opts = {
            "stages": body.get("stages") or ["image", "mesh", "stl"],
            "quality": body.get("quality") or "标准",
            "image_model": body.get("image_model") or "",
            "model_3d": body.get("model_3d") or "",
            "resolution": body.get("resolution") or "",
            "size_mm": body.get("size_mm") or 120,
            "image_path": body.get("image_path") or "",
        }
        j = _new_job(prompt, opts)
        JOB_QUEUE.put(j["id"])
        self.send_response(202)
        loc = f"/v1/jobs/{j['id']}"
        payload = json.dumps({"job_id": j["id"], "status": "queued",
                              "status_url": loc, **{}},
                             ensure_ascii=False).encode()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Location", loc)
        self.end_headers()
        self.wfile.write(payload)
        self._audit(202, note=f"job:{j['id']}")

    def _post_chat(self, body: dict):
        msgs = body.get("messages") or []
        if not msgs:
            return self._problem(400, "missing-messages", "messages 不能为空")
        prompt = "\n".join(str(m.get("content") or "") for m in msgs
                           if isinstance(m, dict))
        from backend import describer
        text = describer._call_ollama(prompt, timeout=int(body.get("timeout") or 180))
        self._send({
            "id": "chatcmpl-" + uuid.uuid4().hex[:16],
            "object": "chat.completion",
            "created": int(time.time()),
            "model": body.get("model") or "forgeform-local",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": text}}],
            "usage": {"prompt_tokens": None, "completion_tokens": None,
                      "total_tokens": None},
        })

    def _post_images(self, body: dict):
        prompt = str(body.get("prompt") or "").strip()
        if not prompt:
            return self._problem(400, "missing-prompt", "prompt 不能为空")
        size = str(body.get("size") or "1024x1024")
        res = size.replace("x", "×") if "x" in size else "1024×1024"
        pipe = pipeline()
        cfg = pipe.config
        key = body.get("model") or ((cfg.get("stages") or {}).get("image") or {}).get(
            "default_model", "")
        results = pipe.generate_images(prompt, count=1, model_key=key,
                                       quality_name=body.get("quality") or "标准",
                                       resolution=res, skip_cache=True)
        ok = [r for r in (results or []) if getattr(r, "success", False) and r.output_path]
        if not ok:
            return self._problem(502, "generation-failed", "本机出图失败")
        fp = Path(ok[0].output_path)
        data = base64.b64encode(fp.read_bytes()).decode()
        self._send({"created": int(time.time()),
                    "data": [{"b64_json": data, "revised_prompt": prompt}]})


# ══════════════════════════════════════════════════════════════════
# 文档
# ══════════════════════════════════════════════════════════════════
def _models() -> dict:
    cfg = config()
    out = {"object": "list", "data": []}
    for kind in ("image", "3d"):
        for k, v in ((cfg.get("models") or {}).get(kind) or {}).items():
            out["data"].append({"id": k, "object": "model",
                                "owned_by": "forgeform-local",
                                "kind": "image" if kind == "image" else "mesh",
                                "name": v.get("name") or k})
    return out


OPENAPI = {
    "openapi": "3.1.0",
    "info": {"title": "ForgeForm Local API",
             "version": "1.0.0",
             "description": "把本机的出图 / 建模 / 对话能力开成 HTTP 接口。"},
    "servers": [{"url": "http://127.0.0.1:7863"}],
    "security": [{"bearerAuth": []}],
    "components": {"securitySchemes": {"bearerAuth": {
        "type": "http", "scheme": "bearer",
        "description": "本机签发的 ff_ 开头 key（tools/api_key.py 创建）"}}},
    "paths": {
        "/v1/health": {"get": {"security": [], "summary": "健康检查",
                               "responses": {"200": {"description": "ok"}}}},
        "/v1/models": {"get": {"summary": "模型清单",
                               "responses": {"200": {"description": "ok"}}}},
        "/v1/generate": {"post": {
            "summary": "提交生成任务（异步）",
            "requestBody": {"required": True, "content": {"application/json": {
                "schema": {"type": "object", "required": ["prompt"], "properties": {
                    "prompt": {"type": "string"},
                    "stages": {"type": "array", "items": {
                        "type": "string", "enum": ["image", "mesh", "stl"]}},
                    "quality": {"type": "string",
                                "enum": ["快速", "标准", "高质量", "极致"]},
                    "image_model": {"type": "string"},
                    "model_3d": {"type": "string"},
                    "size_mm": {"type": "number"}}}}}},
            "responses": {"202": {"description": "已排队", "headers": {
                "Location": {"schema": {"type": "string"}}}}}}},
        "/v1/jobs/{id}": {"get": {"summary": "查任务状态",
                                  "responses": {"200": {"description": "ok"}}}},
        "/v1/jobs/{id}/files/{kind}": {"get": {
            "summary": "下载产物（image / glb / stl）",
            "responses": {"200": {"description": "文件"}}}},
        "/v1/chat/completions": {"post": {"summary": "对话（OpenAI 兼容）",
                                          "responses": {"200": {"description": "ok"}}}},
        "/v1/images/generations": {"post": {
            "summary": "出图（OpenAI 兼容，返回 b64_json）",
            "responses": {"200": {"description": "ok"}}}},
    },
}

DOCS_HTML = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>ForgeForm Local API</title>
<style>
:root{--bg:#0c1019;--panel:#141a27;--line:#273042;--text:#edf1fa;--muted:#9da9bd;--accent:#9ce4da}
body{background:var(--bg);color:var(--text);font:14px/1.7 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;margin:0;padding:40px}
main{max-width:920px;margin:0 auto}
h1{font-size:26px;letter-spacing:.02em;margin:0 0 6px}
p.sub{color:var(--muted);margin:0 0 28px}
section{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:18px 20px;margin-bottom:14px}
h2{font-size:13px;color:var(--accent);letter-spacing:.06em;margin:0 0 10px;font-weight:600}
code{background:rgba(255,255,255,.07);border-radius:6px;padding:1px 6px;font-family:ui-monospace,Consolas,monospace;font-size:12.5px}
pre{background:#080b12;border:1px solid var(--line);border-radius:10px;padding:12px 14px;overflow:auto;font-size:12.5px}
.method{display:inline-block;min-width:52px;font-weight:700;font-size:11px;letter-spacing:.06em}
.get{color:#7cc4ff}.post{color:#9ce4da}
</style></head><body><main>
<h1>ForgeForm Local API</h1>
<p class="sub">本机能力（出图 / 建模 / STL / 对话）开成的 HTTP 接口 · <code>Authorization: Bearer ff_…</code></p>

<section><h2>1. 提交任务</h2>
<p><span class="method post">POST</span> <code>/v1/generate</code></p>
<pre>curl -X POST http://127.0.0.1:7863/v1/generate \\
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \\
  -d '{"prompt":"一个红色小苹果","quality":"高质量","stages":["image","mesh","stl"]}'

→ 202  {"job_id":"a1b2c3d4","status":"queued","status_url":"/v1/jobs/a1b2c3d4"}</pre></section>

<section><h2>2. 查状态</h2>
<p><span class="method get">GET</span> <code>/v1/jobs/{id}</code></p>
<pre>→ {"job_id":"a1b2c3d4","status":"running","stage":"mesh","percent":55.0,
   "elapsed_sec":96.4,"files":{"image":"/v1/jobs/a1b2c3d4/files/image"}}</pre></section>

<section><h2>3. 取产物</h2>
<p><span class="method get">GET</span> <code>/v1/jobs/{id}/files/{image|glb|stl}</code></p>
<pre>curl -H "Authorization: Bearer $KEY" -OJ \\
  http://127.0.0.1:7863/v1/jobs/a1b2c3d4/files/stl</pre></section>

<section><h2>4. OpenAI 兼容</h2>
<p><span class="method post">POST</span> <code>/v1/chat/completions</code> ·
   <span class="method post">POST</span> <code>/v1/images/generations</code> ·
   <span class="method get">GET</span> <code>/v1/models</code></p>
<pre># 任何 OpenAI SDK，把 base_url 指过来就能用
openai.Client(base_url="http://127.0.0.1:7863/v1", api_key="ff_...")</pre></section>

<section><h2>5. 错误</h2>
<p>统一 <code>application/problem+json</code>（RFC 9457）：</p>
<pre>{"type":"https://forgeform.local/err/rate-limited","title":"rate-limited",
 "status":429,"detail":"超过 6 次/分钟"}</pre></section>

<section><h2>6. 完整规范</h2>
<p><a href="/openapi.json" style="color:var(--accent)">/openapi.json</a>（OpenAPI 3.1）</p></section>
</main></body></html>
"""


def main() -> int:
    ap = argparse.ArgumentParser(description="铸想 本机能力 API")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=7863)
    args = ap.parse_args()

    keys = S.list_api_keys()
    if not keys:
        print("⚠️ 还没有签发任何 key。先跑一次：", flush=True)
        print("   PYTHONPATH= python tools/api_key.py issue --label me", flush=True)
        print("（服务仍会启动，但没有 key 的请求一律 401）", flush=True)

    threading.Thread(target=_worker, daemon=True, name="ff-api-worker").start()
    threading.Thread(target=lambda: [(_gc(), time.sleep(60)) for _ in iter(int, 1)],
                     daemon=True, name="ff-api-gc").start()

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"ForgeForm Local API: http://{args.host}:{args.port}", flush=True)
    print(f"  文档   http://{args.host}:{args.port}/docs", flush=True)
    print(f"  OpenAPI http://{args.host}:{args.port}/openapi.json", flush=True)
    print(f"  限流   生成 {RATE_WRITE_PER_MIN} 次/分钟/key · "
          f"查询 {RATE_READ_PER_MIN} 次/分钟/key · 并发 {MAX_CONCURRENT}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n收到中断，退出。", flush=True)
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
