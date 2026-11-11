# -*- coding: utf-8 -*-
"""实时进度跟踪（2026-08-11 v3）
后台线程用 websocket-client（同步）连接 ComfyUI WS，监听 progress 消息（value/max），
供 app 定时器读取显示进度条。掉线静默重连，绝不刷日志。
v3：阶段图标 + ETA 剩余时间估算
"""
import json
import threading
import time

try:
    import websocket  # websocket-client
    HAVE_WS = True
except ImportError:
    HAVE_WS = False

PROGRESS_URL_TMPL = "ws://127.0.0.1:{port}/ws?clientId={}"


def _cfg_ports() -> list:
    """从 config.json 读 ComfyUI 端口（单卡时 image/media 同端口，自动去重）"""
    try:
        import json as _json
        from pathlib import Path as _P
        cfg = _json.loads((_P(__file__).resolve().parent.parent / "config.json").read_text(encoding="utf-8"))
        pc = cfg.get("comfyui_ports", {})
        img = pc.get("image", 8188)
        media = pc.get("media", img)
        return sorted({int(img), int(media)})
    except Exception:
        return [8188]


DEFAULT_PORTS = _cfg_ports()  # 单卡：8188；双卡：8188+8189


class ProgressTracker:
    def __init__(self):
        self._lock = threading.Lock()
        self._progress = 0.0       # 0-1 当前任务进度
        self._running = False      # 是否有任务在跑
        self._node = ""            # 当前节点类型
        self._last_value = 0
        self._last_max = 0
        self._last_ts = 0          # 上次 progress 时间戳（ETA 用）
        self._threads = {}         # port → thread
        self._ws = {}              # port → ws
        self._stop = False
        self._client_id = None     # 绑定 pipeline 的 client_id（ComfyUI 只给提交者推进度）

    def bind(self, client_id: str):
        """绑定 pipeline 的 client_id，进度消息只发给提交任务的连接"""
        with self._lock:
            self._client_id = client_id
        self.stop()
        # 2026-08-12 修复：强制关闭旧 WS，否则旧线程阻塞在 recv，
        # start() 因 is_alive() 被跳过 → 永远收不到进度
        for ws in list(self._ws.values()):
            try:
                ws.close()
            except Exception:
                pass
        self._ws = {}
        time.sleep(0.5)
        self.start()

    def _url(self, port):
        with self._lock:
            cid = self._client_id or "ai-workshop-progress"
        return PROGRESS_URL_TMPL.format(port=port, cid=cid)

    # ─── 线程控制 ───────────────────────────────
    def start(self):
        # 2026-08-12 修复：stop() 后必须能强制重启（旧线程收尾期 is_alive 仍 True，
        # 原逻辑直接 return 导致 _stop 保持 True、WS 永远不重连 → 收不到进度）
        self._stop = False
        for port in DEFAULT_PORTS:
            t = self._threads.get(port)
            if not t or not t.is_alive():
                t = threading.Thread(target=self._run, args=(port,), daemon=True)
                self._threads[port] = t
                t.start()

    def stop(self):
        self._stop = True

    # ─── 后台监听线程（同步，静默容错，每端口一个）──
    def _run(self, port):
        if not HAVE_WS:
            return
        while not self._stop:
            try:
                ws = websocket.create_connection(
                    self._url(port), timeout=15, max_size=2 ** 26,
                )
                # 2026-08-12 修复：recv 加超时，stop() 后线程能及时退出
                ws.settimeout(2)
                self._ws[port] = ws
            except Exception:
                time.sleep(3)
                continue
            try:
                while not self._stop:
                    try:
                        raw = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        continue
                    except Exception:
                        break
                    if not raw:
                        continue
                    try:
                        msg = json.loads(raw)
                    except Exception:
                        continue
                    self._handle(msg)
            except Exception:
                pass
            finally:
                try:
                    ws.close()
                except Exception:
                    pass
            time.sleep(2)

    def _handle(self, msg):
        mtype = msg.get("type")
        data = msg.get("data", {})
        if mtype == "progress":
            with self._lock:
                mx = max(data.get("max", 1), 1)
                self._progress = data.get("value", 0) / mx
                self._last_value = data.get("value", 0)
                self._last_max = data.get("max", 1)
                self._node = str(data.get("node", ""))
                self._running = True
                self._last_ts = time.time()
        elif mtype == "executing":
            node = data.get("node")
            with self._lock:
                if node is None:
                    self._running = False  # 任务结束
                else:
                    self._node = str(node)
                    if not self._running:
                        self._progress = 0.0
        elif mtype == "execution_start":
            with self._lock:
                self._running = True
                self._progress = 0.0
                self._last_value = 0
                self._node = ""

    # ─── 状态读取 ───────────────────────────────
    def snapshot(self) -> dict:
        """返回 {running, progress(0-100), value, max, node, eta_s}"""
        with self._lock:
            eta_s = None
            # ETA 估算：每步平均耗时 × 剩余步
            if self._running and self._last_value > 0 and self._last_max > self._last_value:
                elapsed_total = time.time() - self._last_ts if self._last_ts else 1
                per_step = max(elapsed_total / max(self._last_value, 1), 0.1)
                eta_s = int(per_step * (self._last_max - self._last_value))
            return {
                "running": self._running,
                "progress": round(self._progress * 100),
                "value": self._last_value,
                "max": self._last_max,
                "node": self._node,
                "eta_s": eta_s,
            }

    def progress_html(self) -> str:
        """渲染进度条 HTML v2（阶段图标 + ETA + 微光动画）"""
        s = self.snapshot()
        if not s["running"]:
            return ""
        pct = s["progress"]
        node = s.get("node", "")
        stage_map = {
            "KSampler": ("🎨", "采样生成"),
            "VAEDecode": ("🧬", "解码"),
            "VAEDecodeTripoSplat": ("✨", "高斯解码"),
            "SplatToMesh": ("🔺", "转网格"),
            "TripoSplatConditioning": ("📐", "图像编码"),
            "RemoveBackground": ("✂️", "背景移除"),
            "LoadImage": ("🖼", "加载图像"),
            "SaveGLB": ("💾", "保存模型"),
            "SaveImage": ("💾", "保存图片"),
            "[Comfy3D] Hunyuan3D 21 ShapeGen": ("🧊", "3D 建模"),
            "[Comfy3D] Hunyuan3D 21 TexGen": ("🎨", "贴图生成"),
        }
        icon, stage = stage_map.get(node, ("⏳", f"节点 {node}" if node else "处理中"))
        eta_txt = ""
        if s.get("eta_s") is not None and s["eta_s"] > 0:
            m, sec = divmod(s["eta_s"], 60)
            eta_txt = f' · 预计剩余 {m}分{sec}秒' if m else f' · 预计剩余 {sec}秒'
        return (
            '<div class="gen-progress">'
            f'<div class="gp-top"><span>{icon} {stage}</span>'
            f'<span class="gp-pct">{pct}%</span></div>'
            f'<div class="gp-bar"><div class="gp-fill" style="width:{pct}%"></div></div>'
            f'<div class="gp-sub">{s["value"]} / {s["max"]} 步{eta_txt}</div>'
            '</div>'
        )


# 全局单例
tracker = ProgressTracker()
tracker.start()


def get_progress_html() -> str:
    return tracker.progress_html()
