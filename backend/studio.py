"""Graphical local control service. Standard library only: starts before pip installs."""
from __future__ import annotations
import copy
import json
import re
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]


# ── 国内 pip 源 ────────────────────────────────────────────
# 2026-11-13：国内直连 pypi.org 每个包都要等十几秒到几十秒，装 30 多个包
# 能拖到半小时以上。阿里云实测可用（清华源 403 过）。
# 用 `--extra-index-url` 而不是 `--index-url`：主源仍是 PyPI，
# 阿里云只是加速副本；这样阿里云缺包时不会直接失败。
PIP_CN = "https://mirrors.aliyun.com/pypi/simple/"
# ★ 2026-11-15：不再写死阿里云，改成**当场实测挑一个能用的**。
#   原因是各镜像在不同网络/不同时期差别很大（清华 403 过又恢复、
#   中科大有时最快、腾讯偶尔抽风），写死一个就等于把某一时刻的状态
#   固化成永久假设。netcheck 会把结果缓存 1 小时，不会每次都测。
def _pick_pip_args() -> list[str]:
    try:
        from backend import netcheck
        base = netcheck.pick_pypi() or PIP_CN
    except Exception:
        base = PIP_CN
    host = base.split("//", 1)[-1].split("/", 1)[0]
    return ["-i", base, "--timeout", "60", "--retries", "3",
            "--trusted-host", host]


PIP_ARGS = _pick_pip_args()
# torch 的 cu126 轮子阿里云镜像没有，官方源又慢。策略：先试阿里云，
# 拿不到再回退官方（官方只对 torch 用，2-3 GB，走一次就够）。
TORCH_INDEX = "https://download.pytorch.org/whl/cu126"


def merge(dst, patch):
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(dst.get(key), dict):
            merge(dst[key], value)
        else:
            dst[key] = copy.deepcopy(value)
    return dst


def save_config(root, patch):
    target = root / 'config.json'
    source = target if target.exists() else root / 'config.example.json'
    base = json.loads(source.read_text(encoding='utf-8'))
    merge(base, patch)
    if target.exists():
        shutil.copy2(target, root / 'config.json.bak')
    temp = root / 'config.json.tmp'
    temp.write_text(json.dumps(base, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temp, target)
    return base


class Studio:
    def __init__(self, root=ROOT, app_port=7860):
        self.root = Path(root)
        self.app_port = app_port
        self.lock = threading.RLock()
        self.token = secrets.token_urlsafe(32)
        self.job = {'status': 'idle', 'label': '准备就绪', 'step': 0, 'total': 0, 'logs': [],
                    # 下载进度：大模型几十分钟起步，只给"第几步"用户会以为卡死
                    'file': '', 'percent': 0.0, 'speed_mbps': 0.0,
                    'downloaded_mb': 0.0, 'size_mb': 0.0,
                    'eta_sec': 0, 'tick': 0.0}
        self.cancelled = threading.Event()
        self.child = None
        self.app = None
        self.snapshot = None
        self.plans = {}
        self.model_root = str(self.root / 'models')

    @property
    def python(self):
        candidate = self.root / '.venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
        return str(candidate if candidate.exists() else Path(sys.executable))

    def log(self, line):
        with self.lock:
            self.job['logs'] = (self.job['logs'] + [str(line).strip()])[-150:]

    # fetch_model.py 每 5% 打一行：
    #   62.5%  2275.4/3643.0 MB   3.08 MB/s  (model.fp16.safetensors)
    # fetch_model.py：  62.5%  2275.4/3643.0 MB   3.08 MB/s  (model.fp16.safetensors)
    _PROGRESS_RE = re.compile(
        r"(\d+(?:\.\d+)?)%\s+([\d.]+)/([\d.]+)\s*MB\s+([\d.]+)\s*MB/s\s+\((.+?)\)")
    # ollama pull：     pulling 8934d96d3f08:  45% ▕███▏  1.2 GB/2.7 GB  5.1 MB/s  1m20s
    _OLLAMA_RE = re.compile(
        r"pulling\s+([0-9a-f]{6,})[^:]*:\s+(\d+)%.*?([\d.]+)\s*([GM])B\s*/\s*([\d.]+)\s*([GM])B"
        r"\s+([\d.]+)\s*([GM])B/s")

    @staticmethod
    def _to_mb(v: float, unit: str) -> float:
        return v * 1024.0 if unit == "G" else v

    def _parse_progress(self, line: str) -> None:
        """把下载器的进度行抽成结构化字段，供前端画进度条与剩余时间。

        两种输出格式都要认：模型走 fetch_model.py，对话模型走 `ollama pull`。
        只认前一种的话，装 LLM 那一步界面会一直停在"正在准备 xxx"不动 —— 实测踩过。
        """
        line = line or ""
        with self.lock:
            self.job['tick'] = time.time()
            m = self._PROGRESS_RE.search(line)
            if m:
                pct, done, size, speed, name = m.groups()
                self.job.update(percent=float(pct), downloaded_mb=float(done),
                                size_mb=float(size), speed_mbps=float(speed),
                                file=name.strip())
            else:
                m = self._OLLAMA_RE.search(line)
                if not m:
                    return
                _h, pct, dv, du, sv, su, spv, spu = m.groups()
                done = self._to_mb(float(dv), du)
                size = self._to_mb(float(sv), su)
                speed = self._to_mb(float(spv), spu)
                self.job.update(percent=float(pct), downloaded_mb=done, size_mb=size,
                                speed_mbps=speed, file="(ollama)")
            # 剩余时间：拿剩余量除以当前速度。速度是瞬时值，抖得厉害，夹一下上下限。
            speed = float(self.job.get('speed_mbps') or 0)
            left = float(self.job.get('size_mb') or 0) - float(self.job.get('downloaded_mb') or 0)
            self.job['eta_sec'] = int(left / speed) if speed > 0.05 and left > 0 else 0

    def command(self, argv):
        if self.cancelled.is_set():
            raise RuntimeError('任务已取消，可重试续传')
        env = dict(os.environ, PYTHONPATH='', PYTHONUTF8='1', PYTHONIOENCODING='utf-8', PYTHONUNBUFFERED='1')
        proc = subprocess.Popen(argv, cwd=self.root, env=env, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace',
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.child = proc
        for line in proc.stdout:
            self._parse_progress(line)
            self.log(line)
        code = proc.wait()
        self.child = None
        if self.cancelled.is_set():
            raise RuntimeError('任务已取消，可重试续传')
        if code:
            raise RuntimeError(f'此步骤失败（退出码 {code}），请查看详细记录并重试')

    def start_job(self, label, fn):
        with self.lock:
            if self.job['status'] == 'running':
                raise ValueError('已有任务运行，请等待完成或取消')
            self.cancelled.clear()
            self.job = {'status': 'running', 'label': label, 'step': 0, 'total': 0, 'logs': [],
                        'file': '', 'percent': 0.0, 'speed_mbps': 0.0,
                        'downloaded_mb': 0.0, 'size_mb': 0.0}
        def run():
            try:
                fn()
                with self.lock:
                    self.job['status'] = 'cancelled' if self.cancelled.is_set() else 'done'
                    self.job['label'] = '已取消' if self.cancelled.is_set() else '已完成'
            except Exception as exc:
                self.log(str(exc))
                with self.lock:
                    self.job['status'] = 'cancelled' if self.cancelled.is_set() else 'failed'
                    self.job['label'] = str(exc)
        threading.Thread(target=run, daemon=True).start()
        return {'ok': True}

    def cancel(self):
        self.cancelled.set()
        child = self.child
        if child and child.poll() is None:
            if os.name == 'nt':
                subprocess.run(['taskkill', '/PID', str(child.pid), '/T', '/F'], capture_output=True,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            else:
                child.terminate()
        return {'ok': True}

    def scan(self, model_root=''):
        from backend.backends.detect import probe_all, probe_local_diffusers, guess_comfy_dir, _SCAN_CACHE
        from backend.backends.recommend import probe_hardware, summarize_assets, build_profiles, pick_default, list_llm_choices, inspect_comfy_reuse
        if model_root:
            self.model_root = str(Path(model_root).expanduser().resolve())
        config = self.config()
        comfy = config.get('paths', {}).get('comfyui', '')
        if not comfy or '${' in comfy:
            # 配置里写的是占位符（或压根没写）→ 自动找
            comfy = guess_comfy_dir()
            if comfy:
                self.log(f'自动找到 ComfyUI: {comfy}')
        # A user-initiated rescan must reflect weights installed or removed since the last scan.
        if comfy:
            _SCAN_CACHE.pop(str(Path(comfy).resolve()).lower(), None)
        fws = probe_all(comfy_dir=comfy or None)
        local = probe_local_diffusers([self.model_root])
        fws = [local if f.key == "diffusers" else f for f in fws]
        hardware = probe_hardware(self.model_root)
        assets = summarize_assets(fws)
        if assets.get('comfyui') and assets.get('comfyui_3d'):
            assets['comfyui_reuse_ready'], assets['comfyui_reuse_reason'] = inspect_comfy_reuse(comfy, config, fws)
        profiles = build_profiles(hardware, assets, self.model_root)
        self.plans = {p.key: p for p in profiles}
        default = pick_default(profiles)
        self.snapshot = {'hardware': hardware.to_dict(), 'frameworks': [f.to_dict() for f in fws],
            'profiles': [p.to_dict() for p in profiles], 'default': default.key if default and default.fits else '',
            'model_root': self.model_root, 'llm_choices': list_llm_choices(hardware.vram_gb, hardware.ram_gb), 'scanned_at': time.time()}
        self.log('环境扫描完成，不加载模型')

    def config(self):
        p = self.root / 'config.json'
        return json.loads((p if p.exists() else self.root / 'config.example.json').read_text(encoding='utf-8-sig'))

    def runtime_install(self):
        venv = self.root / '.venv'
        if not (venv / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')).exists():
            self.command([sys.executable, '-m', 'venv', str(venv)])
        # 走国内源；失败再回退默认（回退在 command 的调用方看退出码处理）
        self.command([self.python, '-m', 'pip', 'install', '-r',
                      str(self.root / 'requirements.txt'), *PIP_ARGS])
        self.command([self.python, '-c', 'import gradio,fastapi,trimesh; print("基础环境验证通过")'])
        (venv / '.deps-ok').touch()

    def install(self, key, llm=""):
        # 调试/自检用：把"下载"跳过去，只走流程。默认关闭。
        # 加这个是因为"一键开始会不会真的开下载"只能这样验 —— 否则测一次要下 14 GB。
        if os.environ.get("FORGEFORM_INSTALL_DRYRUN") == "1":
            self.log(f"[dry-run] 跳过下载，方案 = {key}")
            self.job["step"] = self.job["total"]
            return
        if key not in self.plans:
            raise ValueError('请先重新扫描，再选择方案')
        plan = copy.deepcopy(self.plans[key])
        if not plan.fits:
            raise ValueError(plan.reason)
        if llm:
            from backend.backends.recommend import Download, list_llm_choices, _ollama_install_download
            if not self.snapshot:
                raise ValueError('请重新扫描硬件')
            hw = self.snapshot['hardware']
            choice = next((c for c in list_llm_choices(hw['vram_gb'], hw['ram_gb']) if c['model'] == llm), None)
            if not choice or not choice['fits']:
                raise ValueError('所选对话模型不适合当前硬件或不在支持清单中')
            plan.downloads = [d for d in plan.downloads if d.kind != 'llm']
            if not any(d.plugin_id == 'ollama' for d in plan.downloads):
                plan.downloads.insert(0, _ollama_install_download())
            plan.downloads.append(Download('llm', 'chat', llm, size_gb=choice['size_gb']))
            if hw['disk_free_gb'] < sum(d.size_gb for d in plan.downloads) * 1.2 + 5:
                raise ValueError('磁盘空间不足以安装所选对话模型与方案')
            plan.config_patch['agent'] = {'enabled': True, 'provider': 'ollama'}
            plan.config_patch.setdefault('models', {})['llm'] = {'default_model': llm,
                'fallback_model': llm, 'think': False, 'models': {llm: {'num_ctx': 2048 if hw['ram_gb'] < 20 else 4096}}}
        if not (self.root / '.venv' / '.deps-ok').exists():
            self.runtime_install()
        self.job['total'] = len(plan.downloads) + 1
        for i, item in enumerate(plan.downloads):
            self.job.update(step=i, label='正在准备 ' + item.repo)
            if item.kind == 'framework':
                if item.plugin_id == 'ollama':
                    exe = shutil.which('ollama') or str(Path(os.environ.get('LOCALAPPDATA', '')) / 'Programs/Ollama/ollama.exe')
                    if not Path(exe).exists():
                        if not shutil.which('winget'):
                            raise RuntimeError('未找到 winget，无法自动安装 Ollama。请安装 Windows 应用安装程序后重试。')
                        self.command(['winget', 'install', '--id', 'Ollama.Ollama', '--exact', '--silent',
                            '--accept-package-agreements', '--accept-source-agreements'])
                elif item.plugin_id == 'pydeps':
                    self.command([self.python, '-m', 'pip', 'install', 'torch', 'torchvision',
                                  '--index-url', TORCH_INDEX, '--timeout', '120', '--retries', '5',
                                  '--trusted-host', 'download.pytorch.org'])
                    self.command([self.python, '-m', 'pip', 'install', 'diffusers', 'transformers',
                                  'accelerate', 'safetensors', 'sentencepiece',
                                  *PIP_ARGS])
                    # 2026-11-13: GitHub direct often stalls at 0 bytes in China (laptop users
                    # reported "stuck forever"). gh-proxy returns 200 in testing; fall back
                    # to direct only if the proxy fails.
                    _hy = 'https://gh-proxy.com/https://github.com/Tencent-Hunyuan/Hunyuan3D-2/archive/refs/heads/main.zip'
                    try:
                        self.command([self.python, '-m', 'pip', 'install', _hy, *PIP_ARGS])
                    except Exception:
                        if self.cancelled.is_set():
                            raise RuntimeError('任务已取消，可重试续传')
                        self.log('gh-proxy failed for hy3dgen, falling back to GitHub direct...')
                        self.command([self.python, '-m', 'pip', 'install',
                                      'https://github.com/Tencent-Hunyuan/Hunyuan3D-2/archive/refs/heads/main.zip'])
                    self.command([self.python, '-c', 'import torch; from hy3dgen.shapegen import Hunyuan3DDiTFlowMatchingPipeline; assert torch.cuda.is_available(), "CUDA 不可用，请检查显卡驱动"'])
                else:
                    raise ValueError('不支持的框架安装步骤：' + item.plugin_id)
            elif item.kind == 'llm':
                exe = shutil.which('ollama') or str(Path(os.environ.get('LOCALAPPDATA', '')) / 'Programs/Ollama/ollama.exe')
                if not Path(exe).exists():
                    raise RuntimeError('Ollama 安装未完成')
                try:
                    urllib.request.urlopen('http://127.0.0.1:11434/api/tags', timeout=2).close()
                except OSError:
                    subprocess.Popen([exe, 'serve'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                    for _ in range(30):
                        if self.cancelled.wait(.5):
                            raise RuntimeError('任务已取消')
                        try:
                            urllib.request.urlopen('http://127.0.0.1:11434/api/tags', timeout=1).close()
                            break
                        except OSError:
                            continue
                self.command([exe, 'pull', item.repo])
            else:
                cmd = [self.python, '-u', str(self.root / 'tools/fetch_model.py'), '--repo', item.repo,
                    '--dest', item.dest, '--threads', '4']
                for pattern in item.include:
                    cmd += ['--include', pattern]
                for pattern in item.exclude:
                    cmd += ['--exclude', pattern]
                self.command(cmd)
        # Commit only after every step succeeded. Failed downloads never become active.
        save_config(self.root, plan.config_patch)
        self.job['step'] = self.job['total']
        self.log('模型及配置已保存。可以进入创作。')

        # ★ 2026-11-15：装完在本机跑一次实测，把真实吞吐写成样本。
        #   出厂包里不带任何耗时样本（outputs/ 不进发布包）—— 换台机器差几倍，
        #   拿别的机器的数字当基准只会让进度环乱跳。
        #   这里只测对话（约 2 秒）；出图/建模留给用户第一次真跑时自然累积。
        if not self.cancelled.is_set():
            try:
                self.log('本机实测校准（对话吞吐，约 2 秒）…')
                env = dict(os.environ)
                env['PYTHONPATH'] = str(self.root)
                proc = subprocess.run(
                    [self.python, '-u', str(self.root / 'tools/calibrate.py'), '--quick'],
                    cwd=str(self.root), env=env, capture_output=True, text=True,
                    timeout=180)
                tail = [ln for ln in (proc.stdout or '').splitlines() if 'tok/s' in ln]
                if tail:
                    self.log('校准完成：' + tail[-1].strip())
                else:
                    self.log('校准跳过（对话服务未就绪，首次生成时会自动累积）')
            except Exception as exc:
                self.log(f'校准跳过：{type(exc).__name__}')

    def catalog(self):
        from backend.plugins.spec import registry
        config = self.config()
        entries = []
        for p in registry(reload=True).all():
            kind = '3d' if p.kind == 'mesh' else p.kind
            cfg = config.get('models', {}).get(kind, {}).get(p.id, {})
            ref = cfg.get('path') or p.path
            entries.append({'id': p.id, 'kind': kind, 'label': p.label, 'repo': p.repo,
                'unavailable_reason': p.unavailable_reason, 'path': ref, 'installed': bool(ref and Path(ref).exists()), 'vram_hint': p.vram_hint})
        return entries

    def configure(self, data):
        # UI accepts only supported fields, never arbitrary config or shell commands.
        if self.job['status'] == 'running':
            raise ValueError('请等待当前安装完成后再保存')
        mode = data.get('mode', 'auto')
        quant = data.get('quant', 'auto')
        if mode not in ('auto', 'direct', 'comfyui') or quant not in ('auto', 'none', 'fp8', 'bnb8', 'bnb4', 'gguf'):
            raise ValueError('无效的后端或量化方式')
        patch = {'backend': {'mode': mode}, 'models': {}, 'stages': {}}
        from backend.plugins.spec import registry
        reg = registry(reload=True)
        for kind, plugkind in [('image', 'image'), ('3d', 'mesh')]:
            pid = data.get(kind, '')
            if not pid:
                continue
            plugin = reg.get(plugkind, pid)
            existing = self.config().get('models', {}).get(kind, {})
            if not plugin and pid not in existing:
                raise ValueError('未知模型：' + pid)
            if plugin and plugin.unavailable_reason:
                raise ValueError(plugin.unavailable_reason)
            path = data.get(kind + '_path', '').strip()
            if path and not Path(path).exists():
                raise ValueError('模型路径不存在：' + path)
            cfg = {'quant': quant if kind == 'image' else 'none'}
            if path:
                cfg['path'] = str(Path(path).resolve())
            if plugin:
                if plugin.pipeline: cfg['pipeline'] = plugin.pipeline
                if plugin.loader: cfg['loader'] = plugin.loader
            patch['models'][kind] = {pid: cfg}
            patch['stages'][kind] = {'default_model': pid}
        model = data.get('llm', '').strip()
        endpoint = data.get('endpoint', '').strip().rstrip('/')
        if model:
            from urllib.parse import urlsplit
            parsed = urlsplit(endpoint)
            if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError('请输入有效的兼容 API 地址')
            patch['agent'] = {'enabled': True, 'provider': 'strata', 'strata_url': endpoint, 'strata_model': model}
        save_config(self.root, patch)
        return {'ok': True, 'message': '配置已保存；新生成任务使用新模型，对话服务变更在重开创作后生效。'}

    def launch(self):
        if self.app and self.app.poll() is None:
            return
        if not (self.root / '.venv' / '.deps-ok').exists():
            raise RuntimeError('请先在环境页面安装基础环境')
        if not (self.root / 'config.json').exists():
            raise RuntimeError('请先选择方案完成配置')
        # ★ 2026-11-16：起 app 之前先认人 + 清场。
        #   原来的判据是"端口能连上就报错让你自己去关" —— 但占着它的往往就是
        #   **上一次留下的我们自己的 app**（实测会攒出 7860 + 7862 两份，
        #   各自加载一遍模型，显存被劈开）。现在：是我们的就杀掉再起，是别人的才报错。
        from backend import ports as _ports
        _killed = _ports.kill_stale('app.py')
        if _killed:
            self.log(f'已清理 {len(_killed)} 个残留的创作空间进程')
        _who = _ports.who_is(self.app_port)
        if _who['busy'] and not _who['ours']:
            # 别人占着 7860 → 换一个端口继续，而不是把用户挡在"请自己改端口"前面。
            # （原来这里是 raise，实测用户看到报错就以为装坏了。）
            _alt = _ports.pick_free(self.app_port + 2, 20)
            if _alt:
                self.log(f'{self.app_port} 被占用（{_who["hint"]}），创作空间改用 {_alt}')
                self.app_port = _alt
            else:
                raise RuntimeError(f'端口 {self.app_port} 起 20 个都被占用，无法启动')
        logs = self.root / 'logs'
        logs.mkdir(exist_ok=True)
        env = dict(os.environ, PYTHONPATH='', PYTHONUTF8='1', FOUNDRY_PORT=str(self.app_port), FOUNDRY_HOST='127.0.0.1')
        with (logs / 'studio-app.log').open('a', encoding='utf-8') as output:
            self.app = subprocess.Popen([self.python, str(self.root / 'app.py')], cwd=self.root,
                env=env, stdout=output, stderr=subprocess.STDOUT, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        for _ in range(120):
            if self.app.poll() is not None:
                tail = (logs / 'studio-app.log').read_text(encoding='utf-8', errors='replace')[-5000:]
                self.log(tail)
                raise RuntimeError('创作服务启动失败，请展开详细记录')
            try:
                urllib.request.urlopen(f'http://127.0.0.1:{self.app_port}/config', timeout=1).close()
                self.log('创作服务已就绪')
                return
            except OSError:
                time.sleep(.5)
        raise RuntimeError('创作服务启动超时，请查看 logs/studio-app.log')

    # ══════════════════════════════════════════════════════════
    # 删除：单个模型 / 整个铸想
    # ══════════════════════════════════════════════════════════
    #
    # 安全边界（很重要）：
    #   · 只删**铸想自己下的**东西。ComfyUI 的 models 目录是用户的其他资产，
    #     哪怕铸想读它、也不能删它 —— 一旦删了，用户别的工具全废。
    #   · 删除前先算清楚"要删什么、多大"，让界面能列清单给用户确认。
    #   · 不接受前端传路径，只接受 catalog 里的 id / 固定的 scope 名。

    def _deletable_roots(self):
        """铸想自己拥有的、可以安全删除的目录。"""
        m = self.root / 'models'
        return [m] if m.exists() and self._safe_local(m) else []

    def _safe_local(self, path):
        """Only physical children of this installation; never follow reparse points."""
        try:
            relative = path.absolute().relative_to(self.root.absolute())
            if not relative.parts:
                return False
            current = self.root
            for part in relative.parts:
                current = current / part
                if current.is_symlink() or getattr(current.lstat(), 'st_file_attributes', 0) & 0x400:
                    return False
            path.resolve().relative_to(self.root.resolve())
            return True
        except (OSError, ValueError):
            return False

    def _check_delete(self, path):
        if not self._safe_local(path):
            raise ValueError('拒绝删除外部目录或链接：' + str(path))
        if path.is_dir():
            for child in path.rglob('*'):
                if not self._safe_local(child):
                    raise ValueError('目录包含链接，需手动检查：' + str(child))

    def _check_app_stopped(self):
        if self.app and self.app.poll() is None:
            raise ValueError('请先关闭创作进程，再删除数据')

    def _under_deletable(self, path: Path) -> bool:
        """这个路径是否落在铸想可删范围内（resolve 后比较，防 ../ 与符号链接）。"""
        try:
            real = path.resolve()
        except Exception:
            return False
        for root in self._deletable_roots():
            try:
                relative = real.relative_to(root.resolve())
                return bool(relative.parts) and self._safe_local(path)
            except ValueError:
                continue
        return False

    def model_removable(self, model_id: str) -> tuple[bool, str]:
        from backend.plugins.spec import registry
        cfg = self.config()
        for p in registry(reload=True).all():
            kind = '3d' if p.kind == 'mesh' else p.kind
            if p.id != model_id:
                continue
            node = (cfg.get('models') or {}).get(kind, {}).get(p.id, {})
            ref = node.get('path') or p.path
            if not ref:
                return False, '这个模型没有记录本地路径'
            path = Path(os.path.expandvars(str(ref)))
            if not path.exists():
                return False, '文件不存在（可能已经删过）'
            if path.is_file() and not self._under_deletable(path):
                return False, '不在铸想可管理的目录内（可能是 ComfyUI 的共享权重，不做删除）'
            if path.is_dir() and not self._under_deletable(path):
                return False, '不在铸想可管理的目录内（可能是 ComfyUI 的共享权重，不做删除）'
            return True, ''
        return False, '没有这个模型'

    def remove_model(self, model_id: str):
        with self.lock:
            return self._remove_model(model_id)

    def _remove_model(self, model_id: str):
        if self.job['status'] == 'running':
            raise ValueError('请等待当前任务完成后再删除模型')
        self._check_app_stopped()
        ok, why = self.model_removable(model_id)
        if not ok:
            raise ValueError(why)
        from backend.plugins.spec import registry
        cfg = self.config()
        for p in registry(reload=True).all():
            kind = '3d' if p.kind == 'mesh' else p.kind
            if p.id != model_id:
                continue
            node = (cfg.get('models') or {}).get(kind, {}).get(p.id, {})
            ref = node.get('path') or p.path
            path = Path(os.path.expandvars(str(ref)))
            self._check_delete(path)
            freed = self._dir_size(path)
            self.log(f'删除模型 {model_id}: {path}')
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)
            self.log(f'已删除，释放 {freed / 1024 ** 3:.2f} GB')
            return {'ok': True, 'freed_gb': round(freed / 1024 ** 3, 2)}
        raise ValueError('没有这个模型')

    @staticmethod
    def _dir_size(path: Path) -> int:
        try:
            if path.is_file():
                return path.stat().st_size
            total = 0
            for f in path.rglob('*'):
                if f.is_file():
                    total += f.stat().st_size
            return total
        except Exception:
            return 0

    def purge_plan(self):
        """列出"彻底删除铸想"会删掉什么、各多大。界面拿它做确认清单。"""
        items = []
        venv = self.root / '.venv'
        if venv.exists():
            items.append({'key': 'venv', 'label': 'Python 运行环境（.venv）',
                          'path': str(venv), 'size_gb': round(self._dir_size(venv) / 1024 ** 3, 2),
                          'note': '删除后需要重新安装依赖才能再用'})
        for r in self._deletable_roots():
            items.append({'key': 'models', 'label': '模型权重',
                          'path': str(r), 'size_gb': round(self._dir_size(r) / 1024 ** 3, 2),
                          'note': 'ComfyUI 的共享权重不在此列'})
        for key, label, note in (
            ('outputs', '生成结果与模型库', '含已生成的图片/GLB/STL，删了不可恢复'),
            ('logs', '日志', '排查问题用'),
            ('temp', '临时文件', ''),
            ('projects', '对话组存档', '每个对话组的 project.json 与候选图'),
        ):
            d = self.root / key
            if d.exists():
                items.append({'key': key, 'label': label, 'path': str(d),
                              'size_gb': round(self._dir_size(d) / 1024 ** 3, 2),
                              'note': note})
        cfg = self.root / 'config.json'
        if cfg.exists():
            items.append({'key': 'config', 'label': '配置文件', 'path': str(cfg),
                          'size_gb': 0.0, 'note': '删了下次启动会重新生成'})
        return {'items': items,
                'total_gb': round(sum(i['size_gb'] for i in items), 2)}

    def purge(self, scope: str = 'all'):
        """删掉铸想自己装的东西。scope: all | runtime | models | outputs。

        **不删**：ComfyUI 目录、用户指定的外部模型目录、仓库源码本身。
        """
        self._check_app_stopped()
        scope = (scope or 'all').lower()
        if scope not in ('all', 'runtime', 'models', 'outputs'):
            raise ValueError('未知清理范围')
        selected = {'all': None, 'runtime': {'venv'}, 'models': {'models'}, 'outputs': {'outputs', 'logs', 'temp', 'projects'}}[scope]
        for item in self.purge_plan()['items']:
            if selected is None or item['key'] in selected:
                self._check_delete(Path(item['path']))
        removed = []
        if scope in ('all', 'runtime'):
            venv = self.root / '.venv'
            if venv.exists():
                freed = self._dir_size(venv)
                shutil.rmtree(venv)
                removed.append(('venv', round(freed / 1024 ** 3, 2)))
        if scope in ('all', 'models'):
            for r in self._deletable_roots():
                if r.exists():
                    freed = self._dir_size(r)
                    shutil.rmtree(r)
                    removed.append((str(r), round(freed / 1024 ** 3, 2)))
        if scope in ('all', 'outputs'):
            for key in ('outputs', 'logs', 'temp', 'projects'):
                d = self.root / key
                if d.exists():
                    freed = self._dir_size(d)
                    shutil.rmtree(d)
                    removed.append((key, round(freed / 1024 ** 3, 2)))
        if scope == 'all':
            cfg = self.root / 'config.json'
            if cfg.exists():
                cfg.unlink(missing_ok=True)
                removed.append(('config.json', 0.0))
        total = round(sum(g for _, g in removed), 2)
        self.log(f'已删除：{removed}，共释放 {total} GB')
        return {'ok': True, 'removed': removed, 'freed_gb': total}

    def netboost_advice(self):
        """下载链路体检 + 要不要提示开 Steam++。

        国内直连 GitHub Release 实测 0.17 MB/s，模型 3–9 GB 的话要几小时。
        这个接口只是**给出判断**，不会自动装任何软件。
        """
        try:
            from . import netboost
            return {'ok': True, **netboost.advise()}
        except Exception as exc:
            return {'ok': False, 'error': f'{type(exc).__name__}: {exc}'}

    def netboost_pack(self):
        """查 Steam++ 的最新安装包地址（不下载，只给界面显示版本和体积）。"""
        try:
            from . import netboost
            return {'ok': True, **netboost.latest_installer()}
        except Exception as exc:
            return {'ok': False, 'error': f'{type(exc).__name__}: {exc}'}

    def netboost_install(self):
        """下载并静默安装 Steam++。

        ★ 只在用户点了确认之后才会走到这里 —— 界面上是个明确的"安装"按钮。
          装软件是显式动作，不做静默自动装。
        """
        try:
            from . import netboost
            info = netboost.latest_installer()
            if not info.get('ok'):
                return {'ok': False, 'error': info.get('error') or '查不到安装包'}
            self.log(f"开始下载 {info['name']}（{info['size_mb']} MB）…")
            res = netboost.install_steampp(progress=self._log)
            self.log(f"安装结果: {'成功' if res.get('ok') else res.get('error')}")
            return {'ok': bool(res.get('ok')), **res}
        except Exception as exc:
            return {'ok': False, 'error': f'{type(exc).__name__}: {exc}'}

    def netboost_launch(self):
        """启动本机已装的 Steam++。没装就返回下载地址，不静默安装。"""
        try:
            from . import netboost
            return {'ok': True, **netboost.launch()}
        except Exception as exc:
            return {'ok': False, 'error': f'{type(exc).__name__}: {exc}'}

    def hardware(self):
        """实时硬件快照。给界面的监控面板用，每 2 秒拉一次。

        数据来自 backend/monitor.py 的 HardwareMonitor（同一个实例维护历史曲线），
        所以连续调用会拿到滚动的 cpu_hist / vram_hist 等序列，前端直接画折线。
        采集失败时返回 ok=False + 原因，不让监控把界面拖死。
        """
        try:
            from .monitor import get_stats
            snap = get_stats()
            return {'ok': True, 'ts': time.time(), **snap}
        except Exception as exc:
            return {'ok': False, 'ts': time.time(),
                    'error': f'{type(exc).__name__}: {exc}'}

    def status(self):
        with self.lock:
            return {'job': copy.deepcopy(self.job), 'snapshot': self.snapshot,
                'ready': bool(self.app and self.app.poll() is None),
                'app_url': f'http://127.0.0.1:{self.app_port}',
                'runtime_ready': (self.root / '.venv' / '.deps-ok').exists()}


def make_server(studio, port=7861):
    class Handler(BaseHTTPRequestHandler):
        def send(self, data, status=200, mime='application/json'):
            body = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', mime + '; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            # ★ 2026-11-17 身份头：这台机器上可能并存两份铸想（旧版 D:\AI-Workshop
            #   和现在这份）。带根目录才能区分"自己人"和"另一份安装"，
            #   否则新装的会被判成"已经在跑"直接退出。
            try:
                from backend.ports import ROOT_HEADER, ROOT_TAG
                self.send_header(ROOT_HEADER, ROOT_TAG)
            except Exception:
                pass
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def trusted(self):
            host = self.headers.get('Host', '')
            allowed = {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}
            origin = self.headers.get('Origin')
            return host in allowed and (not origin or origin in {'http://' + h for h in allowed})

        def do_GET(self):
            if not self.trusted(): return self.send({'error': '无效来源'}, 403)
            try:
                self.path = self.path.split('?', 1)[0]   # 路由只认路径，query 单存
                self._query = self.path.split('?', 1)[1] if '?' in self.path else ''
                if self.path == '/api/ping':
                    # 免鉴权探活：前端据此区分"服务器没连上"和"点了没反应"
                    return self.send({'ok': True, 'app': 'forgeform-studio',
                                      'version': (studio.root / 'VERSION').read_text(
                                          encoding='utf-8').strip(),
                                      'ts': __import__('time').time()})
                if self.path == '/api/status': return self.send(studio.status())
                if self.path == '/api/hardware': return self.send(studio.hardware())
                if self.path == '/api/netboost': return self.send(studio.netboost_advice())
                if self.path == '/api/netboost/pack': return self.send(studio.netboost_pack())
                if self.path == '/api/purge-plan': return self.send(studio.purge_plan())
                if self.path == '/api/firstrun':
                    # 三条链就绪度 + 候选路径（给"一键开始"用）
                    from backend import firstrun
                    return self.send(firstrun.status())
                if self.path == '/api/doctor':
                    # 环境 + 网络自检（联网约 10 秒；?offline=1 只查本地）
                    from urllib.parse import parse_qs
                    q = parse_qs(getattr(self, '_query', '') or '')
                    offline = q.get('offline', ['0'])[0] in ('1', 'true', 'yes')
                    from backend import netcheck
                    env = netcheck.env_checks()
                    out = {'env': env,
                           'bad': [e['name'] for e in env if not e['ok']]}
                    if not offline:
                        net = netcheck.probe_all()
                        out['net'] = {k: net.get(k) for k in ('pypi', 'github', 'hf')}
                        out['picks'] = {'pypi': netcheck.pick_pypi(),
                                        'github': netcheck.pick_github(),
                                        'hf': netcheck.pick_hf()}
                    return self.send(out)
                if self.path == '/api/location':
                    # 安装位置：当前在哪、推荐在哪、模型放哪
                    from backend import location as _loc
                    return self.send(_loc.report())
                if self.path == '/api/keys':
                    from backend import secrets as _sec
                    return self.send({'keys': _sec.list_api_keys(),
                                      'crypto': _sec.backend_name(),
                                      'weak': _sec.is_weak(),
                                      'default_ttl_days': _sec.DEFAULT_TTL_DAYS})
                if self.path == '/api/cloud':
                    from backend import cloud
                    return self.send(cloud.status())
                if self.path == '/api/maintenance':
                    from backend import maintenance as mt
                    return self.send({'status': mt.status(), 'plan': mt.plan(),
                                      'summary': mt.retention_report()})
                if self.path == '/api/catalog': return self.send({'models': studio.catalog(), 'config': {
                    'stages': studio.config().get('stages', {}), 'backend': studio.config().get('backend', {})}})
                if self.path in ('/', '/index.html'):
                    html = (studio.root / 'ui/studio.html').read_text(encoding='utf-8')
                    ver = '3.0.0'
                    try:
                        ver = (studio.root / 'VERSION').read_text(encoding='utf-8').strip()
                    except Exception:
                        pass
                    html = html.replace('__TOKEN__', studio.token).replace('__VERSION__', ver)
                    return self.send(html.encode(), mime='text/html')
                if self.path.split('?')[0] in ('/studio.css', '/studio.js'):
                    # 2026-11-16：支持 /studio.js?v=3.7.0 这种查询串。
                    # 加版本号的目的是**升级后不吃旧缓存** —— 用户换个版本
                    # 打开还是旧 JS，就会出现"点了没反应"（事件绑在旧代码上）。
                    _name = self.path.split('?')[0].lstrip('/')
                    p = studio.root / 'ui' / _name
                    return self.send(p.read_bytes(), mime='text/css' if p.suffix == '.css' else 'text/javascript')
                return self.send({'error': '未找到'}, 404)
            except Exception as exc:
                self.send({'error': str(exc)}, 500)

        def do_POST(self):
            if not self.trusted() or not secrets.compare_digest(self.headers.get('X-Studio-Token', ''), studio.token):
                return self.send({'error': '请刷新页面后重试'}, 403)
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if size > 16384: raise ValueError('请求过大')
                data = json.loads(self.rfile.read(size) or b'{}')
                if self.path == '/api/scan': result = studio.start_job('检测硬件与已有环境', lambda: studio.scan(data.get('model_root', '')))
                elif self.path == '/api/install': result = studio.start_job('准备安装方案', lambda: studio.install(data.get('key', ''), data.get('llm', '')))
                elif self.path == '/api/runtime': result = studio.start_job('安装基础环境', studio.runtime_install)
                elif self.path == '/api/launch': result = studio.start_job('启动创作空间', studio.launch)
                elif self.path == '/api/firstrun':
                    # mode: auto（默认，自己挑最快的） / 指定某个候选 key
                    #
                    # 关键：如果挑中的方案**需要下载**，这里直接把下载作业开起来，
                    # 而不是"告诉用户去另一个页面点开始"。用户点一次就够 ——
                    # 界面此刻已经能用，下载在后台跑，跑完再自动应用配置。
                    from backend import firstrun
                    mode = (data.get('mode') or 'auto').strip()
                    result = firstrun.auto() if mode == 'auto' else firstrun.apply(mode)
                    if (result.get('need_download') and result.get('ok')
                            and data.get('start_download', True)
                            and studio.job.get('status') not in ('running', 'queued')):
                        prof = result.get('profile') or 'starter'
                        job = studio.start_job(f'下载 {result.get("name") or prof}',
                                               lambda: studio.install(prof))
                        result['download_started'] = bool(job)
                        result['download_profile'] = prof
                    result['status'] = firstrun.status()
                    if data.get('launch') and result.get('ok') and not result.get('need_download'):
                        studio.launch()
                    result['ok'] = bool(result.get('ok', True))
                    return self.send(result)
                elif self.path == '/api/location':
                    # 应用自动选择（只改"模型放哪"，不动程序本体）
                    from backend import location as _loc
                    result = _loc.apply()
                    result['report'] = _loc.report()
                elif self.path == '/api/keys':
                    # 动作：issue / revoke / delete / delete-all
                    from backend import secrets as _sec
                    act = (data.get('action') or 'list').strip()
                    out = {'ok': True}
                    if act == 'issue':
                        # ttl=0 表示永久，不能写 `data.get('ttl') or 默认值`
                        # —— 0 是 falsy，会被悄悄改成 90 天（实测踩到）。
                        _ttl = data.get('ttl')
                        _ttl = _sec.DEFAULT_TTL_DAYS if _ttl is None else int(_ttl)
                        rec = _sec.issue_api_key(data.get('label') or 'default',
                                                 ttl_days=_ttl)
                        out['token'] = rec['token']   # 只回这一次
                        out['id'] = rec['id']
                        out['expires_at'] = rec['expires_at']
                    elif act == 'revoke':
                        out['count'] = _sec.revoke_api_key(key_id=data.get('id') or '')
                    elif act == 'delete':
                        out['deleted'] = _sec.delete_api_key(data.get('id') or '')
                    elif act == 'delete-all':
                        out['count'] = _sec.delete_all_api_keys()
                    else:
                        return self.send({'error': '未知操作'}, 400)
                    out['keys'] = _sec.list_api_keys()
                    result = out
                elif self.path == '/api/cloud':
                    # 写云端配置 / 存 key。key 走 DPAPI 加密，不回显。
                    from backend import cloud
                    from backend import secrets as _sec_store
                    use = (data.get('use') or '').strip()
                    if data.get('key'):
                        _sec_store.put_secret(data.get('key_name') or use, data['key'])
                    cfg = studio.config()
                    cloud_cfg = cfg.setdefault('cloud', {})
                    cloud_cfg.setdefault('enabled', True)
                    if use:
                        section = cloud_cfg.setdefault(use, {})
                        for k in ('provider', 'base_url', 'model', 'key_name',
                                  'mode', 'timeout'):
                            if data.get(k) is not None:
                                section[k] = data[k]
                        section['enabled'] = bool(data.get('enabled', True))
                    save_config(studio.root, {'cloud': cloud_cfg})
                    result = {'ok': True, 'cloud': cloud.status()}
                elif self.path == '/api/maintenance':
                    from backend import maintenance as mt
                    result = mt.run(apply=bool(data.get('apply')))
                elif self.path == '/api/calibrate':
                    # 2026-11-15：本机实测校准。deep=true 才跑完整链路（出图/建模/STL，
                    # 几分钟，要求模型已装且显存空闲）；默认只测对话吞吐（约 2 秒）。
                    _deep = bool(data.get('deep'))
                    _argv = [studio.python, '-u', str(studio.root / 'tools' / 'calibrate.py')]
                    _argv += [] if _deep else ['--quick']
                    result = studio.start_job('完整实跑校准' if _deep else '校准对话吞吐',
                                              lambda: studio.command(_argv))
                elif self.path == '/api/cancel': result = studio.cancel()
                elif self.path == '/api/config': result = studio.configure(data)
                elif self.path == '/api/netboost': result = studio.netboost_launch()
                elif self.path == '/api/netboost/install': result = studio.start_job('安装 Steam++', studio.netboost_install)
                elif self.path == '/api/remove': result = studio.remove_model(data.get('id', ''))
                elif self.path == '/api/purge': result = studio.start_job('删除铸想数据', lambda: studio.purge(data.get('scope', 'all')))
                else: return self.send({'error': '未找到'}, 404)
                return self.send(result)
            except (ValueError, RuntimeError) as exc:
                return self.send({'error': str(exc)}, 400)
            except Exception as exc:
                return self.send({'error': str(exc)}, 500)
        def log_message(self, *_): pass
    return ThreadingHTTPServer(('127.0.0.1', port), Handler)
