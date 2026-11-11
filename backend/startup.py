"""开机自检 + 自愈模块 — APP 独立运行的核心
启动时检查：目录 / 服务(ComfyUI/Ollama) / 依赖 / 模型 / GPU / 文件库
发现问题自动修复：建目录、拉起服务、清理端口冲突
"""
import os
import re
import json
import sys
import time
import subprocess
import importlib
from pathlib import Path
from dataclasses import dataclass

import requests

# ═══ 路径配置 ═══
from backend.env import ROOT as APP_DIR, COMFYUI_DIR as COMFY_DIR, COMFY_PYTHON
# 微调 agent 服务（serve_agent.py）：跑在 finetune 自己的 venv 里（有 unsloth/peft）
TUNED_DIR = APP_DIR / "finetune"
TUNED_VENV_PYTHON = TUNED_DIR / "venv" / "Scripts" / "python.exe"
OUTPUT_DIR = APP_DIR / "outputs"
TEMP_DIR = APP_DIR / "temp"
LOG_DIR = APP_DIR / "logs"

# 必须存在的目录
REQUIRED_DIRS = [
    OUTPUT_DIR, OUTPUT_DIR / "images", OUTPUT_DIR / "videos",
    OUTPUT_DIR / "3d", OUTPUT_DIR / "stl",
    TEMP_DIR, LOG_DIR,
]

COMFY_ENV = {
    "PYTHONPATH": "",          # 防止 Hermes venv PIL 污染 ComfyUI subprocess
    # 2026-09-22 修「开创意工坊卡在启动 ComfyUI」：
    # 启动脚本用 Start-Process -RedirectStandardOutput 打开的是 GBK(cp936) 文件流，
    # 没设这两个变量时 ComfyUI 的 app/logger.py 一写入 emoji(🎉) 就抛
    # UnicodeEncodeError，异常从 print() 冒出 → **在绑定端口之前进程就死了**，
    # 于是 8188 永不监听、启动脚本空等超时。强制 UTF-8 后不再触发。
    "PYTHONIOENCODING": "utf-8",
    "PYTHONUTF8": "1",
    "HF_ENDPOINT": "https://hf-mirror.com",
    "MODEL_CHECKSUM_DISABLED": "1",
    "HF_HUB_DISABLE_XET": "1",
    # GPU 安全（2026-08-04 规范）：限制单次分配 + 线程数（08-20：双路 96 核，OMP 提到 16）
    "PYTORCH_CUDA_ALLOC_CONF": "max_split_size_mb:2048,expandable_segments:True",
    "OMP_NUM_THREADS": "16",
    "MKL_NUM_THREADS": "16",
}
COMFY_URL = "http://127.0.0.1:8188"
COMFY_MEDIA_URL = "http://127.0.0.1:8188"  # 2026-09 单卡：视频/3D 复用图片实例（GPU1 已拆，不再 8189）
OLLAMA_URL = "http://127.0.0.1:11434"


# ─── 资源协调（2026-10-16 新增）───────────────────────────────
#
# 宇凯要求："跑铸想的时候、或者跑视频生成等占用大的本地部署时，
#   先检查有没有别的任务在跑，以及显存/内存/CPU 是否可用；
#   并且跑完之后及时卸载显存内存，以便跑后续项目。"
#
# 统一实现放在 ZYKT 侧的 tools/resource-guard.mjs（那里能看到全部项目）。
# 铸想这边只调用它，不重复实现一套判断逻辑（否则两边的阈值会漂）。
#
# ★ 为什么用子进程调 CLI 而不是 import Python 版：
#   协调器要同时探 8188/8082/7860/8791 四个服务，还要读 nvidia-smi、
#   维护账本 —— 这些逻辑只有一份（在 ZYKT 仓库里），
#   铸想只要"问一句""就够了。用 CLI 的退出码当判据最省事：
#     0 = 可以跑   1 = 有任务在跑，要等   2 = 资源不足，要先释放
RESOURCE_GUARD = Path(r"D:\ZYKT-studio\tools\resource-guard.mjs")
ZYKT_DIR = Path(r"D:\ZYKT-studio")
TSX = ZYKT_DIR / "node_modules" / ".bin" / "tsx.cmd"

# CLI 退出码 → action。★ 必须有这张表：
# 少了它时 fallback 分支会把 exit=2（资源不足）误判成 "wait"，
# 于是"该先释放"被说成"该等别的任务"，建议完全错。
_EXIT_TO_ACTION = {
    0: "ok",              # 资源充足
    1: "wait",            # 有别的任务在跑 —— 等它跑完，不抢
    2: "release-first",   # 资源不够 —— 先温卸载再跑
}


def _resource_plan(need_vram_mb: int, need_ram_mb: int = 0):
    """问资源协调器"现在能不能起一个吃这么多资源的东西"。

    ★ 永不抛异常 —— 协调器本身坏了不该阻止铸想启动（降级为"不检查"）。
      返回 None 表示"问不到"，调用方应视作"继续但心里有数"。
    """
    try:
        if not RESOURCE_GUARD.exists() or not TSX.exists():
            return None
        out = subprocess.run(
            [str(TSX), str(RESOURCE_GUARD), "check",
             "--vram", str(int(need_vram_mb)), "--ram", str(int(need_ram_mb))],
            cwd=str(ZYKT_DIR), capture_output=True, text=True, timeout=45,
            encoding="utf-8", errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        text = (out.stdout or "") + (out.stderr or "")
        # 从输出里抠出判定结论（CLI 同时用退出码表达，这里取人能看的那句）
        #
        # ⚠️ 这里踩过两个坑，都记下来免得再犯：
        #   ① 原来写 reason = (m.group(2) if m else text.splitlines()[-1:] or [""])[0]
        #      匹配成功时 m.group(2) 是**字符串**，再取 [0] 拿到的是第一个字符
        #      （实测得到 '资'）。两分支类型必须一致。
        #   ② 字符类原来写的是 \w+ —— 它**不含连字符**，而判定词里有
        #      "RELEASE-FIRST"，于是整条匹配失败、悄悄走了 fallback，
        #      报出来的原因是"内存…"（实际是显存不足），方向完全错。
        #      → 必须写成 [\w-]+
        m = re.search(r"判定：([\w-]+)\s*—\s*(.+)", text)
        if m:
            action = m.group(1).lower()
            reason = m.group(2).strip()
        else:
            action = _EXIT_TO_ACTION.get(out.returncode, "wait")
            lines = [l for l in text.strip().splitlines() if l.strip()]
            reason = lines[-1].strip() if lines else "(协调器无输出)"
        return {"action": action, "reason": reason, "exit": out.returncode}
    except Exception as e:
        print(f"[resource] 资源检查失败（忽略，继续启动）: {e!r}", flush=True)
        return None


# 关键模型（实际路径）
REQUIRED_MODELS = [
    ("FLUX FP8", COMFY_DIR / "models/diffusion_models/flux1-dev-fp8.safetensors", False),
    ("FLUX CLIP-L", COMFY_DIR / "models/text_encoders/clip_l.safetensors", False),
    ("FLUX T5XXL", COMFY_DIR / "models/text_encoders/t5xxl_fp8_e4m3fn.safetensors", False),
    ("FLUX VAE", COMFY_DIR / "models/vae/ae.safetensors", False),
    ("H3 Ref2VA int8", COMFY_DIR / "models/diffusion_models/minimax_h3_ref2va_pruned_int8_convrot.safetensors", False),
    ("H3 Turbo LoRA", COMFY_DIR / "models/loras/minimax_h3_turbo_4step_pruned_comfyui.safetensors", False),
    ("Hunyuan3D DiT", COMFY_DIR / "models/diffusion_models/hunyuan3d-dit-v2-1_fp16.ckpt", False),
    ("Hunyuan3D VAE", COMFY_DIR / "models/diffusion_models/hunyuan3d-vae-v2-1_fp16.ckpt", False),
]

REQUIRED_PACKAGES = [
    ("gradio", "Gradio UI"),
    ("trimesh", "STL 转换"),
    ("PIL", "图像处理"),
    ("psutil", "系统监控"),
    ("numpy", "数值计算"),
    ("requests", "HTTP"),
]


@dataclass
class CheckResult:
    name: str
    status: str  # ok / warn / fail / fixed
    message: str
    required: bool = True


def _read_agent_cfg() -> dict:
    """读 config.json 的 agent 段（provider/tuned_url/…），读不到就当 ollama"""
    try:
        cfg = json.loads((APP_DIR / "config.json").read_text(encoding="utf-8"))
        return cfg.get("agent", {}) or {}
    except Exception:
        return {}


class StartupChecker:
    def __init__(self):
        self.results: list[CheckResult] = []
        self.fixed = []

    # ─── 主入口 ───────────────────────────────
    def check_all(self) -> list[CheckResult]:
        self.results = []
        self._check_dirs()
        self._check_packages()
        self._check_comfyui()
        self._check_ollama()
        self._check_tuned_agent()
        self._check_models()
        self._check_gpu()
        self._check_file_library()
        return self.results

    # ─── 目录检查 + 自动创建 ──────────────────
    def _check_dirs(self):
        missing = [d for d in REQUIRED_DIRS if not d.exists()]
        if not missing:
            self.results.append(CheckResult("目录结构", "ok", "全部就绪"))
            return
        created = []
        for d in missing:
            try:
                d.mkdir(parents=True, exist_ok=True)
                created.append(d.name)
            except Exception as e:
                self.results.append(CheckResult("目录结构", "fail", f"{d}: {e}"))
        if created:
            self.fixed.append(f"创建目录: {', '.join(created)}")
            self.results.append(CheckResult("目录结构", "fixed", f"已创建 {len(created)} 个缺失目录"))

    # ─── Python 包 ────────────────────────────
    def _check_packages(self):
        missing = []
        for pkg, desc in REQUIRED_PACKAGES:
            try:
                importlib.import_module(pkg if pkg != "PIL" else "PIL")
            except ImportError:
                missing.append(pkg)
        if not missing:
            self.results.append(CheckResult("依赖包", "ok", f"{len(REQUIRED_PACKAGES)} 个就绪"))
        else:
            self.results.append(CheckResult("依赖包", "warn", f"缺少: {', '.join(missing)}", required=False))

    # ─── ComfyUI 检查 + 自动启动 ────────────────
    def _check_comfyui(self):
        if self._comfy_online():
            # ★ 2026-10-16 修正：原来这里写的是"在线 (8188+8189 双卡)"，
            #   8189 早在 2026-09 拆卡时就撤了，日志会误导排查。
            self.results.append(CheckResult("ComfyUI", "ok", "在线 (8188)"))
            return

        """
        ★ 2026-10-16：启动前先问资源协调器。
        宇凯要求："跑铸想的时候先检查有没有别的任务在跑以及显存内存 CPU 是否可用"。
        这里与自检解耦 —— 资源不够只是**警告 + 排队建议**，
        不阻止铸想本身启动（界面能开、能看历史、能改配置，都是有用的）。
        真正的重活（出图/建模）在调用前另有检查。
        """
        r = _resource_plan(20000, 8000)
        if r is not None and r.get("action") != "ok":
            self.results.append(CheckResult(
                "资源检查", "warn",
                f"{r.get('reason', '资源紧张')}（ComfyUI 仍会尝试启动，但生成可能失败）",
                required=False,
            ))
            self.warnings = getattr(self, "warnings", []) + [r.get("reason", "")]

        # 尝试自动启动
        self.results.append(CheckResult("ComfyUI", "warn", "离线 → 尝试自动启动 (8188)"))
        started = self._start_comfyui()
        if started:
            self.fixed.append("ComfyUI 已自动启动")
            self.results.append(CheckResult("ComfyUI", "fixed", "已自动启动 (8188)"))
        else:
            self.results.append(CheckResult("ComfyUI", "fail", "启动失败，请手动检查"))

    def _comfy_online(self) -> bool:
        # 单卡模式：只用 8188（2026-09 GPU 从双卡变单卡，8189 已撤）
        try:
            return requests.get(f"{COMFY_URL}/system_stats", timeout=3).status_code == 200
        except Exception:
            return False

    # ─── 微调 agent 服务检查 + 自动启动（provider=tuned 时才有意义）──
    def ensure_tuned_service(self):
        """只做「微调对话服务」这一件事：探活，离线就自动拉起（不做 ComfyUI 那一堆重活）。

        为什么要单独一个入口（2026-09-23 宇凯："对话加载太慢，最好一开就常驻在内存"）：
        bootstrap 需要**先**把对话服务拉起来并预热（用户马上就会说话），
        而 check_all() 里 ComfyUI 的自检可能要 60s+ —— 排在它后面等于让用户干等。
        """
        try:
            return self._check_tuned_agent()
        except Exception as e:
            print(f"[bootstrap] 微调服务探活/拉起异常: {e!r}")
            return None

    def _check_tuned_agent(self):
        """agent 走微调模型时，那个 Flask+PEFT 服务（serve_agent.py）必须在线。

        以前它是人工起的：双击 start-all.ps1 起来的 app 根本不知道有这回事，
        于是 provider=tuned 但服务不在 → agent 每轮都静默回退关键词路径（看起来"变笨了"）。
        这里按 ComfyUI/Ollama 的同一套做：探活 → 不在就自动拉起（模型由首次请求懒加载）。
        """
        cfg = _read_agent_cfg()
        if cfg.get("provider", "ollama") != "tuned":
            self.results.append(CheckResult("微调 agent 服务", "ok",
                                            "未启用（provider=ollama，走本地大模型）"))
            return
        url = cfg.get("tuned_url", "http://127.0.0.1:8082")
        if self._http_ok(f"{url}/health"):
            self.results.append(CheckResult("微调 agent 服务", "ok", f"在线 {url}"))
            return
        self.results.append(CheckResult("微调 agent 服务", "warn", f"离线 → 尝试自动启动 {url}"))
        if self._start_tuned_agent(url):
            self.results.append(CheckResult("微调 agent 服务", "fixed",
                                            f"已启动 {url}（模型首次请求时懒加载）"))
            self.fixed.append(f"启动微调 agent 服务 {url}")
        else:
            self.results.append(CheckResult("微调 agent 服务", "fail",
                                            "启动失败 → agent 会回退关键词路径"))

    def _http_ok(self, url: str, timeout: float = 3.0) -> bool:
        try:
            return requests.get(url, timeout=timeout).status_code == 200
        except Exception:
            return False

    def _start_tuned_agent(self, url: str) -> bool:
        """后台起 finetune/serve_agent.py（独立进程，日志进 logs/）"""
        port = re.sub(r"\D", "", url.rsplit(":", 1)[-1]) or "8082"
        py = TUNED_VENV_PYTHON
        script = TUNED_DIR / "serve_agent.py"
        if not py.exists() or not script.exists():
            self.results.append(CheckResult("微调 agent 服务", "fail",
                                            f"缺解释器或脚本：{py} / {script}"))
            return False
        env = dict(os.environ)
        # 关键：清掉 PYTHONPATH —— 否则 venv 的 python 会去加载 Hermes venv 里的 torch，
        # 直接 DLL 加载失败（这个坑踩过多次）
        env.pop("PYTHONPATH", None)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        env["DSH_AGENT_PORT"] = port
        env.setdefault("HF_ENDPOINT", "https://hf-mirror.com")   # gpt-oss 侧要取 MXFP4 内核
        log_dir = Path(__file__).parent.parent / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        out_f = open(log_dir / "tuned-agent.log", "w", encoding="utf-8", errors="replace")
        try:
            subprocess.Popen([str(py), str(script)], cwd=str(TUNED_DIR), env=env,
                             stdout=out_f, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL,
                             creationflags=subprocess.CREATE_NO_WINDOW)
        except Exception as e:
            self.results.append(CheckResult("微调 agent 服务", "fail", f"启动异常: {e}"))
            out_f.close()
            return False
        for _ in range(20):                     # 最多等 20s（起进程很快，模型是懒加载）
            time.sleep(1)
            if self._http_ok(f"{url}/health", timeout=2):
                return True
        return False

    def _port_open(self, port: int = 8188, timeout: float = 1.0) -> bool:
        """快速 TCP 探活（比 Get-NetTCPConnection / HTTP 都快，用于轮询）"""
        import socket
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=timeout):
                return True
        except Exception:
            return False

    def _start_comfyui(self) -> bool:
        """
        ⚠️⚠️ 2026-10-16 重要修正：**不再无条件清杀 main.py 进程**。

        原来的清理逻辑是：
            Where-Object { $_.CommandLine -match 'main.py.*(8188|8189)' } | Stop-Process

        这个条件会杀掉**任何**监听 8188 的 ComfyUI —— 包括：
          · 宇凯自己手动起的（带 --disable-cuda-malloc 那份）
          · 资源协调器起的
          · 已经正常在服务、只是这一瞬间没应答的实例

        实测后果（2026-10-16）：一次自检把它认成"离线"，
        先把健康的实例杀了、再拉起一个新的；而新实例与旧实例参数一致时
        还会出现"两个进程抢 8188、谁都起不来"的窗口。
        更糟的是把外部实例杀了之后，那边并不知道自己的实例没了。

        ★ 现在改成：**只清"我们自己记下的 PID"**。
          真正已在这个端口服务的外部实例一律尊重，不动它。
        """
        # 只清自己上次启动失败留下的残留（读自己的 pid 文件）
        try:
            pidfile = Path(__file__).parent.parent / "logs" / "comfyui-8188.pid"
            if pidfile.exists():
                old = pidfile.read_text(encoding="utf-8").strip()
                if old.isdigit():
                    subprocess.check_output(
                        ["powershell", "-NoProfile", "-Command",
                         f"Stop-Process -Id {old} -Force -ErrorAction SilentlyContinue"],
                        timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
                pidfile.unlink(missing_ok=True)
        except Exception:
            pass
        time.sleep(1)
        env = dict(os.environ)
        env.update(COMFY_ENV)
        # 实例 A：GPU0 → 8188（图片 + 视频 + 3D 单卡复用）
        env["CUDA_VISIBLE_DEVICES"] = "0"

        # 2026-09-22：输出重定向到日志文件。原来直接继承父进程句柄 ——
        # 父进程 stdout 若是管道/GBK 流，ComfyUI 启动日志写满缓冲区就会阻塞，
        # 且崩溃后无日志可查（这正是"卡在开启 ComfyUI"却看不到原因的老坑）。
        log_dir = Path(__file__).parent.parent / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        out_f = open(log_dir / "comfyui-8188-stdout.log", "w", encoding="utf-8", errors="replace")
        err_f = open(log_dir / "comfyui-8188-stderr.log", "w", encoding="utf-8", errors="replace")
        try:
            proc = subprocess.Popen(
                [str(COMFY_PYTHON), "main.py", "--listen", "0.0.0.0", "--port", "8188",
                 # ══════════════ 这两个参数是 V100 上的速度关键，别删 ══════════════
                 # ① --disable-cuda-malloc （必须项，2026-10-14 实测）
                 #    torch 2.0+ 起 ComfyUI 默认启用 cudaMallocAsync，设备名会变成
                 #    "Tesla PG500-216 : cudaMallocAsync"。但 Volta(CC 7.0) 上这套
                 #    异步分配器不被支持 —— KSampler 一跑就抛：
                 #      torch.AcceleratorError: CUDA error: operation not supported
                 #    而这行报在 comfy/samplers.py 的 calculate_sigmas(...).to(device)，
                 #    是**异步**报错点、不是真凶（加 CUDA_LAUNCH_BLOCKING=1 报错位置
                 #    完全不变），照它去查会一路查错方向（踩过多轮）。
                 #    加上后设备名变回 " : native"，出图恢复正常。
                 #
                 # ② 去掉 --lowvram + 加 --use-pytorch-cross-attention （2026-10-14 实测提速）
                 #    ★ --lowvram 在 32GB 的 V100 上完全没必要，而且代价极大：
                 #      它让权重常驻内存、每个算子现搬，实测**出图 183s → 63s（约 3 倍）**。
                 #      同一工作流对照（z_image 512×512/8步）：
                 #        带 --lowvram:      冷 72.6s  热 75.5s
                 #        去掉 --lowvram:    冷 24.7s  热  3.0s   ← 热跑 25 倍
                 #      现在这张卡 32GB，diffusion 7GB + 文本编码器 8.9GB + VAE 装得下，
                 #      3D 建模阶段（Hunyuan3D + WanVAE，日志显示 22.4GB usable）也在范围内。
                 #      铸想每次生成前会先 POST /unload 把对话模型卸掉，不会和 11GB 的
                 #      agent 抢显存。
                 #    ★ --use-pytorch-cross-attention：这张卡上 xformers 不可用
                 #      （日志 "xFormers is not available"）、sageattention 也废了
                 #      （装的是 +cu132torch2.13 版，换 torch 2.9 后 DLL 加载失败）。
                 #      torch 自带的 SDPA 在 V100 + torch 2.9.1+cu128 上实测可用，
                 #      比默认回退路径快。（这一项没单独隔离测量，是和 ① 一起上的。）
                 # ═══════════════════════════════════════════════════════════════
                 "--disable-cuda-malloc", "--use-pytorch-cross-attention"],
                cwd=str(COMFY_DIR), env=env,
                stdout=out_f, stderr=err_f, stdin=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            # ★ 2026-10-16：记下自己的 PID —— 下次要清理时**只清自己起的这个**，
            #   不再无条件杀所有 main.py（那个条件会误杀外部实例，见上面注释）。
            try:
                (log_dir / "comfyui-8188.pid").write_text(str(proc.pid), encoding="utf-8")
            except Exception:
                pass
        except Exception as e:
            self.results.append(CheckResult("ComfyUI", "fail", f"启动异常(8188): {e}"))
            out_f.close(); err_f.close()
            return False

        # 等待就绪：最多 240s（冷启实测约 110s；自定义节点多时更久）
        # 每 5s 打印进度（原来静默等 120s，看起来就是"卡住"）
        print("[bootstrap] ComfyUI 启动中（最多 240s）…", flush=True)
        for i in range(48):
            time.sleep(5)
            # 进程已退出 → 立刻失败并给出日志尾部，不再空等
            if proc.poll() is not None:
                out_f.close(); err_f.close()
                tail = self._log_tail(log_dir / "comfyui-8188-stderr.log")
                self.results.append(CheckResult("ComfyUI", "fail",
                                   f"启动即退出(code={proc.returncode})，见 logs/comfyui-8188-stderr.log"))
                print(f"[bootstrap] ❌ ComfyUI 启动即退出(code={proc.returncode})，日志尾部：\n{tail}",
                      flush=True)
                return False
            if self._port_open(8188):
                out_f.close(); err_f.close()
                print(f"[bootstrap] ✅ ComfyUI 8188 就绪（{(i+1)*5}s）", flush=True)
                return True
            if (i + 1) % 6 == 0:
                print(f"[bootstrap] … 等待 ComfyUI 就绪 {(i+1)*5}s/240s", flush=True)
        out_f.close(); err_f.close()
        return False

    @staticmethod
    def _log_tail(path: Path, n: int = 15) -> str:
        try:
            return "\n".join(path.read_text(encoding="utf-8", errors="replace")
                             .strip().splitlines()[-n:])
        except Exception:
            return "(无法读取日志)"

    # ─── Ollama 检查 + 自动启动 ───────────────
    def _check_ollama(self):
        if self._ollama_online():
            self.results.append(CheckResult("Ollama", "ok", "在线 (11434)"))
            return
        self.results.append(CheckResult("Ollama", "warn", "离线 → 尝试自动启动..."))
        started = self._start_ollama()
        if started:
            self.fixed.append("Ollama 已自动启动")
            self.results.append(CheckResult("Ollama", "fixed", "已自动启动 (11434)"))
        else:
            self.results.append(CheckResult("Ollama", "warn", "启动失败（扩写功能不可用）", required=False))

    def _ollama_online(self) -> bool:
        try:
            return requests.get(f"{OLLAMA_URL}/api/tags", timeout=3).status_code == 200
        except Exception:
            return False

    def _start_ollama(self) -> bool:
        try:
            # 2026-09：Hermes gateway 会注入 OLLAMA_HOST=11435，但 app 硬编码 11434。
            # 这里强制 serve 监听 11434，与 _ollama_online 的检查端口对齐。
            env = dict(os.environ)
            env["OLLAMA_HOST"] = "127.0.0.1:11434"
            subprocess.Popen(["ollama", "serve"], env=env, creationflags=subprocess.CREATE_NO_WINDOW)
            for _ in range(10):
                time.sleep(2)
                if self._ollama_online():
                    return True
            return False
        except Exception:
            return False

    # ─── 模型文件 ─────────────────────────────
    def _check_models(self):
        missing = [name for name, path, req in REQUIRED_MODELS if not path.exists()]
        found = len(REQUIRED_MODELS) - len(missing)
        if not missing:
            self.results.append(CheckResult("模型文件", "ok", f"{found}/{len(REQUIRED_MODELS)} 就绪"))
        else:
            self.results.append(CheckResult(
                "模型文件", "warn",
                f"{found}/{len(REQUIRED_MODELS)} — 缺少: {', '.join(missing[:4])}", required=False))

    # ─── GPU ──────────────────────────────────
    def _check_gpu(self):
        """双卡检查：温度(取最高) + 显存(聚合) + 功率墙(220W 铁律)。"""
        try:
            out = subprocess.check_output(
                ["nvidia-smi", "--query-gpu=name,temperature.gpu,memory.used,memory.total,power.limit",
                 "--format=csv,noheader,nounits"],
                stderr=subprocess.DEVNULL, timeout=5,
            ).decode().strip()
            cards = []
            max_temp = 0
            used_mb = total_mb = 0
            power_limit = 0
            for line in out.splitlines():
                if not line.strip():
                    continue
                p = [x.strip() for x in line.split(",")]
                name, temp, used, total = p[0], int(p[1]), int(p[2]), int(p[3])
                lim = int(float(p[4])) if len(p) > 4 else 0
                cards.append(f"{name} {used//1024}G/{total//1024}G·{temp}°C")
                max_temp = max(max_temp, temp)
                used_mb += used
                total_mb += total
                power_limit = max(power_limit, lim)
            msg = " + ".join(cards) + f" | 显存 {used_mb//1024}/{total_mb//1024}G"
            if max_temp >= 83:
                msg += " ⚠️ 高温（≥83°C 会崩）"
                self.results.append(CheckResult("GPU", "warn", msg))
            else:
                self.results.append(CheckResult("GPU", "ok", msg))
            # 功率墙检查（2026-08-20 宇凯指定 300W；>300 说明还是默认 320W 未限）
            if power_limit and power_limit > 300:
                self.results.append(CheckResult(
                    "GPU功率墙", "warn",
                    f"当前 {power_limit}W 未限！崩溃风险：请跑 scripts/set_power_limit.ps1（管理员）"))
            else:
                self.results.append(CheckResult("GPU功率墙", "ok", f"{power_limit}W（宇凯指定 300W）"))
        except FileNotFoundError:
            self.results.append(CheckResult("GPU", "fail", "nvidia-smi 不可用", required=False))
        except Exception as e:
            self.results.append(CheckResult("GPU", "warn", str(e), required=False))

    # ─── 文件库 ───────────────────────────────
    def _check_file_library(self):
        try:
            from backend.file_library import get_all_files
            files = get_all_files()
            self.results.append(CheckResult("文件库", "ok", f"{len(files)} 个文件可访问"))
        except Exception as e:
            self.results.append(CheckResult("文件库", "warn", f"扫描异常: {e}", required=False))

    # ─── 渲染 ─────────────────────────────────
    def log_summary(self, only_interesting: bool = True) -> None:
        """把自检结果打进日志（app 的 bootstrap 调用）。

        只打 warn/fail/fixed：全 ok 时静默，免得每次启动刷一堆正常行。
        自动拉起失败（fail）必须能看见 —— 否则用户只会感到"agent 好像变笨了"，
        根本不知道微调服务没起来、正静默回退关键词路径。
        """
        rows = [r for r in self.results if not only_interesting or r.status != "ok"]
        for r in rows:
            icon = {"ok": "✅", "warn": "⚠️", "fail": "❌", "fixed": "🔧"}.get(r.status, "•")
            print(f"[bootstrap] {icon} {r.name}: {r.message}", flush=True)
        if self.fixed and not rows:
            print(f"[bootstrap] 🔧 本次自动修复: {'；'.join(self.fixed)}", flush=True)

    def format_html(self) -> str:
        items = []
        for r in self.results:
            icon = {"ok": "✅", "warn": "⚠️", "fail": "❌", "fixed": "🔧"}.get(r.status, "•")
            items.append(
                f'<div class="boot-item boot-{r.status}">'
                f'<span class="boot-icon">{icon}</span>'
                f'<span class="boot-name">{r.name}</span>'
                f'<span class="boot-msg">{r.message}</span>'
                f'</div>'
            )
        fixes = ""
        if self.fixed:
            fixes = '<div class="boot-fixes">🔧 本次自动修复: ' + "；".join(self.fixed) + "</div>"
        all_ok = all(r.status != "fail" for r in self.results)
        status_cls = "boot-ready" if all_ok else "boot-error"
        status_text = "✅ 自检通过，系统就绪" if all_ok else "⚠️ 自检完成，部分问题需注意"
        return (
            f'<div class="boot-status {status_cls}">{status_text}</div>'
            f'<div class="boot-fixes">{fixes}</div>'
            + "".join(items)
        )


def run_startup_check() -> tuple:
    """执行完整自检，返回 (checker, ready)"""
    checker = StartupChecker()
    checker.check_all()
    return checker, all(r.status != "fail" for r in checker.results)
