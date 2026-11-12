"""Graphical local control service. Standard library only: starts before pip installs."""
from __future__ import annotations
import copy
import json
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
        self.job = {'status': 'idle', 'label': '准备就绪', 'step': 0, 'total': 0, 'logs': []}
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

    def command(self, argv):
        if self.cancelled.is_set():
            raise RuntimeError('任务已取消，可重试续传')
        env = dict(os.environ, PYTHONPATH='', PYTHONUTF8='1', PYTHONIOENCODING='utf-8', PYTHONUNBUFFERED='1')
        proc = subprocess.Popen(argv, cwd=self.root, env=env, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace',
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.child = proc
        for line in proc.stdout:
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
            self.job = {'status': 'running', 'label': label, 'step': 0, 'total': 0, 'logs': []}
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
        from backend.backends.detect import probe_all, probe_local_diffusers
        from backend.backends.recommend import probe_hardware, summarize_assets, build_profiles, pick_default
        if model_root:
            self.model_root = str(Path(model_root).expanduser().resolve())
        config = self.config()
        comfy = config.get('paths', {}).get('comfyui', '')
        if '${' in comfy:
            comfy = os.environ.get('COMFYUI_DIR', '')
        fws = probe_all(comfy_dir=comfy or None)
        local = probe_local_diffusers([self.model_root])
        fws = [local if f.key == "diffusers" else f for f in fws]
        hardware = probe_hardware(self.model_root)
        assets = summarize_assets(fws)
        profiles = build_profiles(hardware, assets, self.model_root)
        self.plans = {p.key: p for p in profiles}
        default = pick_default(profiles)
        self.snapshot = {'hardware': hardware.to_dict(), 'frameworks': [f.to_dict() for f in fws],
            'profiles': [p.to_dict() for p in profiles], 'default': default.key if default and default.fits else '',
            'model_root': self.model_root, 'scanned_at': time.time()}
        self.log('环境扫描完成，不加载模型')

    def config(self):
        p = self.root / 'config.json'
        return json.loads((p if p.exists() else self.root / 'config.example.json').read_text(encoding='utf-8'))

    def runtime_install(self):
        venv = self.root / '.venv'
        if not (venv / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')).exists():
            self.command([sys.executable, '-m', 'venv', str(venv)])
        self.command([self.python, '-m', 'pip', 'install', '-r', str(self.root / 'requirements.txt')])
        self.command([self.python, '-c', 'import gradio,fastapi,trimesh; print("基础环境验证通过")'])
        (venv / '.deps-ok').touch()

    def install(self, key):
        if key not in self.plans:
            raise ValueError('请先重新扫描，再选择方案')
        plan = copy.deepcopy(self.plans[key])
        if not plan.fits:
            raise ValueError(plan.reason)
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
                    self.command([self.python, '-m', 'pip', 'install', 'torch', 'torchvision', '--index-url', 'https://download.pytorch.org/whl/cu128'])
                    self.command([self.python, '-m', 'pip', 'install', 'diffusers', 'transformers', 'accelerate', 'safetensors', 'sentencepiece'])
                    self.command([self.python, '-m', 'pip', 'install', 'https://github.com/Tencent-Hunyuan/Hunyuan3D-2/archive/refs/heads/main.zip'])
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

    def catalog(self):
        from backend.plugins.spec import registry
        config = self.config()
        entries = []
        for p in registry(reload=True).all():
            kind = '3d' if p.kind == 'mesh' else p.kind
            cfg = config.get('models', {}).get(kind, {}).get(p.id, {})
            ref = cfg.get('path') or p.path
            entries.append({'id': p.id, 'kind': kind, 'label': p.label, 'repo': p.repo,
                'path': ref, 'installed': bool(ref and Path(ref).exists()), 'vram_hint': p.vram_hint})
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
        import socket
        with socket.socket() as s:
            if s.connect_ex(('127.0.0.1', self.app_port)) == 0:
                raise RuntimeError(f'端口 {self.app_port} 被其他服务使用，请关闭旧铸想或修改端口')
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
                if self.path == '/api/status': return self.send(studio.status())
                if self.path == '/api/catalog': return self.send({'models': studio.catalog(), 'config': {
                    'stages': studio.config().get('stages', {}), 'backend': studio.config().get('backend', {})}})
                if self.path in ('/', '/index.html'):
                    html = (studio.root / 'ui/studio.html').read_text(encoding='utf-8').replace('__TOKEN__', studio.token)
                    return self.send(html.encode(), mime='text/html')
                if self.path in ('/studio.css', '/studio.js'):
                    p = studio.root / 'ui' / self.path[1:]
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
                elif self.path == '/api/install': result = studio.start_job('准备安装方案', lambda: studio.install(data.get('key', '')))
                elif self.path == '/api/runtime': result = studio.start_job('安装基础环境', studio.runtime_install)
                elif self.path == '/api/launch': result = studio.start_job('启动创作空间', studio.launch)
                elif self.path == '/api/cancel': result = studio.cancel()
                elif self.path == '/api/config': result = studio.configure(data)
                else: return self.send({'error': '未找到'}, 404)
                return self.send(result)
            except (ValueError, RuntimeError) as exc:
                return self.send({'error': str(exc)}, 400)
            except Exception as exc:
                return self.send({'error': str(exc)}, 500)
        def log_message(self, *_): pass
    return ThreadingHTTPServer(('127.0.0.1', port), Handler)
