import json
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

from backend.backends.recommend import Hardware, build_profiles
from backend.plugins.spec import registry
from backend.studio import Studio, save_config
from tools import fetch_model, updater

ROOT = Path(__file__).resolve().parents[1]


class InstallationRegressionTest(unittest.TestCase):
    def test_launch_after_install_starts_configured_app(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / '.venv').mkdir(); (root / '.venv/.deps-ok').touch()
            (root / 'config.json').write_text('{}')
            studio = Studio(root)
            with patch('backend.studio.subprocess.Popen') as popen, patch('socket.socket') as sock, patch('backend.studio.urllib.request.urlopen'):
                sock.return_value.__enter__.return_value.connect_ex.return_value = 1
                popen.return_value.poll.return_value = None
                studio.launch()
                self.assertEqual(popen.call_args.args[0][-1], str(root / 'app.py'))

    def test_every_plan_filter_compiles_and_mesh_matches_download(self):
        for vram in (8, 16, 32):
            for assets in ({}, {'diffusers': True}, {'comfyui': True, 'comfyui_3d': True}):
                for plan in build_profiles(Hardware(vram, vram, 64, 200, 'GPU'), assets):
                    for download in plan.downloads:
                        for pattern in download.include + download.exclude:
                            re.compile(pattern)
                    if plan.key in ('minimal', 'standard', 'starter'):
                        mesh = next(d for d in plan.downloads if d.kind == 'mesh')
                        self.assertEqual(mesh.plugin_id, plan.mesh_model)
                        self.assertEqual(plan.config_patch['stages']['3d']['default_model'], mesh.plugin_id)
                        self.assertEqual(Path(plan.config_patch['models']['3d'][mesh.plugin_id]['path']), Path(mesh.dest))
                        self.assertFalse(registry().get('mesh', mesh.plugin_id).unavailable_reason)

    def test_explicit_llm_is_pulled_and_configured_only_after_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'config.example.json').write_text('{}')
            (root / '.venv').mkdir(); (root / '.venv/.deps-ok').touch()
            studio = Studio(root)
            hw = Hardware(24, 24, 64, 200, 'GPU')
            studio.snapshot = {'hardware': hw.to_dict()}
            studio.plans = {p.key: p for p in build_profiles(hw, {}, str(root / 'models'))}
            choice = 'qwen3.5:9b-q4_K_M'
            with patch.object(studio, 'command') as command, patch('backend.studio.shutil.which', return_value=sys.executable), patch('backend.studio.urllib.request.urlopen'):
                studio.install('starter', choice)
                self.assertIn([sys.executable, 'pull', choice], [c.args[0] for c in command.call_args_list])
            self.assertEqual(studio.config()['models']['llm']['default_model'], choice)
            self.assertEqual(studio.config()['agent']['provider'], 'ollama')
            before = (root / 'config.json').read_bytes()
            with patch.object(studio, 'command', side_effect=RuntimeError('network failed')):
                with self.assertRaises(RuntimeError): studio.install('starter', choice)
            self.assertEqual((root / 'config.json').read_bytes(), before)
            with self.assertRaises(ValueError): studio.install('starter', 'unknown-model')
            studio.snapshot['hardware']['ram_gb'] = 8
            with self.assertRaises(ValueError): studio.install('starter', choice)

    def test_unimplemented_plugins_cannot_be_selected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root / 'config.example.json').write_text('{}')
            for name in ('trellis2', 'trellis2_gguf'):
                with self.assertRaisesRegex(ValueError, '运行时'):
                    Studio(root).configure({'3d': name})
            entries = Studio(root).catalog()
            self.assertTrue(next(e for e in entries if e['id'] == 'trellis2')['unavailable_reason'])


class DeleteRegressionTest(unittest.TestCase):
    def test_external_shared_models_survive_purge(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'app'; root.mkdir()
            shared = Path(tmp) / 'shared'; shared.mkdir()
            sentinel = shared / 'other-app-model.bin'; sentinel.write_bytes(b'keep')
            (root / 'config.example.json').write_text('{}')
            save_config(root, {'model_root': str(shared), 'models': {'image': {'sd15': {'path': str(shared)}}}})
            studio = Studio(root)
            with patch.dict('os.environ', {'FOUNDRY_MODELS': str(shared)}):
                self.assertFalse(studio._under_deletable(shared))
                with self.assertRaises(ValueError): studio.remove_model('sd15')
                studio.purge('models')
            self.assertEqual(sentinel.read_bytes(), b'keep')

    def test_local_root_cannot_be_removed_as_single_model_and_failures_surface(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root / 'config.example.json').write_text('{}')
            models = root / 'models'; models.mkdir()
            (models / 'weights.bin').write_bytes(b'data')
            studio = Studio(root)
            self.assertFalse(studio._under_deletable(models))
            with patch('backend.studio.shutil.rmtree', side_effect=PermissionError('locked')):
                with self.assertRaises(PermissionError): studio.purge('models')
            self.assertTrue(models.exists())
            studio.app = Mock(); studio.app.poll.return_value = None
            with self.assertRaises(ValueError): studio.purge('models')

    def test_junction_is_not_owned(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root / 'config.example.json').write_text('{}')
            model = root / 'models'; model.mkdir()
            real_stat = model.lstat()
            fake_stat = Mock(wraps=real_stat); fake_stat.st_file_attributes = 0x400
            fake_stat.st_mode = real_stat.st_mode
            with patch.object(Path, 'lstat', return_value=fake_stat):
                self.assertEqual(Studio(root)._deletable_roots(), [])


class DownloadRegressionTest(unittest.TestCase):
    def test_cli_downloads_exact_bytes_and_rejects_escape(self):
        payload = b'model weights' * 100
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200); self.send_header('Content-Length', str(len(payload)))
                self.end_headers(); self.wfile.write(payload)
            def log_message(self, *_): pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                dest = Path(tmp) / 'model'
                args = ['fetch', '--repo', 'test/model', '--dest', str(dest), '--mirror', 'hf', '--exclude', r'\.ckpt$']
                with patch.object(sys, 'argv', args), patch.dict(fetch_model.MIRRORS, {'hf': f'http://127.0.0.1:{server.server_port}'}), patch.object(fetch_model, 'list_files', return_value=[('unet/weights.bin', len(payload)), ('old.ckpt', 3)]):
                    self.assertEqual(fetch_model.main(), 0)
                    self.assertEqual((dest / 'unet/weights.bin').read_bytes(), payload)
                    self.assertFalse((dest / 'old.ckpt').exists())
                with patch.object(sys, 'argv', args), patch.object(fetch_model, 'list_files', return_value=[('../escape.bin', 3)]):
                    self.assertEqual(fetch_model.main(), 1)
                with patch.object(sys, 'argv', args + ['--exclude', '*.ckpt']), patch.object(fetch_model, 'list_files') as listing:
                    with self.assertRaises(SystemExit): fetch_model.main()
                    listing.assert_not_called()
        finally:
            server.shutdown(); server.server_close(); thread.join()

    def test_ignored_resume_range_does_not_append_whole_file(self):
        response = Mock(); response.status = 200; response.headers = {'Content-Length': '10'}
        response.__enter__ = Mock(return_value=response); response.__exit__ = Mock(return_value=False)
        with tempfile.TemporaryDirectory() as tmp:
            part = Path(tmp) / 'part'; part.write_bytes(b'123')
            with patch.object(fetch_model.urllib.request, 'urlopen', return_value=response):
                with self.assertRaises(RuntimeError): fetch_model._fetch_range('http://localhost/model', 0, 9, part, threading.Event())
            self.assertEqual(part.read_bytes(), b'123')


class UpdateRegressionTest(unittest.TestCase):
    def test_merge_preserves_user_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            src, dst = Path(tmp)/'new', Path(tmp)/'old'; src.mkdir(); dst.mkdir()
            for name in ('config.json', 'models/custom.json', 'outputs/result.txt', '.venv/.deps-ok'):
                for folder, value in ((src, 'new'), (dst, 'user')):
                    path = folder / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_text(value)
            (src/'VERSION').write_text('3.0.2'); (src/'app.py').write_text('# new')
            updater.merge(src, dst)
            self.assertEqual((dst/'VERSION').read_text(), '3.0.2')
            self.assertEqual((dst/'app.py').read_text(), '# new')
            for name in ('config.json', 'models/custom.json', 'outputs/result.txt', '.venv/.deps-ok'):
                self.assertEqual((dst/name).read_text(), 'user')

    @unittest.skipUnless(shutil.which('powershell') or shutil.which('pwsh'), 'PowerShell required')
    def test_updater_resolves_actual_release_asset(self):
        # Execute the real resolver with a mocked GitHub API; stop before any download.
        prefix = (ROOT/'update.ps1').read_text(encoding='utf-8').split('$tmp = Join-Path')[0]
        prefix = prefix.replace('$root = Split-Path -Parent $MyInvocation.MyCommand.Path', "$root = (Get-Location).Path")
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp)/'check.ps1'
            prelude = """function Invoke-RestMethod {
              return @{tag_name='v9.8.7'; assets=@(@{name='forgeform-v9.8.7.zip'; browser_download_url='https://github.com/TIANKONGskyy/forgeform/releases/download/v9.8.7/forgeform-v9.8.7.zip';size=42})}
            }
            """
            # param must remain first in the script.
            prefix = prefix.replace("$ErrorActionPreference = 'Stop'", prelude + "$ErrorActionPreference = 'Stop'")
            script.write_text(prefix + "\nif ($zipName -ne 'forgeform-v9.8.7.zip' -or $sources[0] -ne $asset.browser_download_url) { throw 'wrong asset' }", encoding='utf-8-sig')
            result = subprocess.run([shutil.which('powershell') or shutil.which('pwsh'), '-NoProfile', '-NonInteractive', '-File', str(script)], capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
