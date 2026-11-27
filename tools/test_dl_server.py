# -*- coding: utf-8 -*-
"""本地 HTTP 测试服务器：验证并行下载的并发、续传、校验（2026-11-27）。

用标准库 http.server，支持 Range，可以按需控速，用来在没有外网的情况下
测「多文件 × 多连接」到底有没有真的并行。
"""
from __future__ import annotations

import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

FILES: dict[str, bytes] = {}
SLOW = {"bps": 0}          # 每个连接每秒字节数上限，0 = 不限
HITS: dict[str, int] = {}  # 每个路径被请求次数（验证是否真的分块拉）


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send_range(self, body: bytes, path: str) -> None:
        HITS[path] = HITS.get(path, 0) + 1
        rng = self.headers.get("Range")
        start, end = 0, len(body) - 1
        if rng and rng.startswith("bytes="):
            part = rng.split("=", 1)[1]
            s, _, e = part.partition("-")
            start = int(s) if s else 0
            end = int(e) if e else len(body) - 1
            end = min(end, len(body) - 1)
        chunk = body[start:end + 1]
        self.send_response(206 if rng else 200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(len(chunk)))
        if rng:
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(body)}")
        self.end_headers()
        bps = SLOW.get("bps") or 0
        if bps:
            step = max(1024, bps // 10)
            for i in range(0, len(chunk), step):
                self.wfile.write(chunk[i:i + step])
                time.sleep(0.1)
        else:
            self.wfile.write(chunk)

    def do_HEAD(self):
        path = self.path.lstrip("/")
        body = FILES.get(path)
        if body is None:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()

    def do_GET(self):
        path = self.path.lstrip("/")
        body = FILES.get(path)
        if body is None:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        try:
            self._send_range(body, path)
        except (BrokenPipeError, ConnectionResetError):
            pass


def start(files: dict[str, int], slow_bps: int = 0) -> tuple[ThreadingHTTPServer, str]:
    """files: {路径: 字节数}；返回 (server, base_url)。"""
    FILES.clear()
    HITS.clear()
    for name, size in files.items():
        FILES[name] = os.urandom(size) if size < 4 * 1024 * 1024 else bytes(size)
    SLOW["bps"] = slow_bps
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{port}"
