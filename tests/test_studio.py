import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from backend.studio import Studio, make_server, save_config
from backend.backends.recommend import Hardware, build_profiles, Download, Profile
from backend.backends.quant import choose_quant, recommend_quant


class StudioTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / 'config.example.json').write_text(json.dumps({'backend': {'mode': 'auto'}, 'models': {}}))
        (self.root / 'config.json').write_text(json.dumps({'backend': {'mode': 'auto'}, 'keep': 'original'}))
        (self.root / '.venv').mkdir()
        (self.root / '.venv/.deps-ok').touch()
        self.studio = Studio(self.root)
    def tearDown(self):
        self.temp.cleanup()
    def test_failed_install_preserves_config(self):
        before = (self.root / 'config.json').read_bytes()
        self.studio.plans['test'] = Profile('test', 'test', '', 'direct', downloads=[Download('image', 'test', 'a/b', str(self.root / 'models'))], config_patch={'backend': {'mode': 'direct'}})
        with patch.object(self.studio, 'command', side_effect=RuntimeError('download failed')):
            with self.assertRaises(RuntimeError): self.studio.install('test')
        self.assertEqual((self.root / 'config.json').read_bytes(), before)
    def test_success_commits_patch_and_backup(self):
        self.studio.plans['test'] = Profile('test', 'test', '', 'direct', config_patch={'backend': {'mode': 'direct'}})
        self.studio.install('test')
        self.assertEqual(self.studio.config()['backend']['mode'], 'direct')
        self.assertEqual(self.studio.config()['keep'], 'original')
        self.assertEqual(json.loads((self.root / 'config.json.bak').read_text())['backend']['mode'], 'auto')
    def test_bad_path_does_not_change_configuration(self):
        before = (self.root / 'config.json').read_bytes()
        with self.assertRaises(ValueError): self.studio.configure({'image': 'sd15', 'image_path': str(self.root / 'missing')})
        self.assertEqual(before, (self.root / 'config.json').read_bytes())
    def test_existing_model_configuration_is_applied(self):
        model = self.root / 'model'; model.mkdir()
        self.studio.configure({'mode': 'direct', 'image': 'sd15', 'image_path': str(model), 'quant': 'fp8'})
        cfg = self.studio.config()
        self.assertEqual(cfg['stages']['image']['default_model'], 'sd15')
        self.assertEqual(cfg['models']['image']['sd15']['quant'], 'fp8')
    def test_write_requires_origin_and_token(self):
        server = make_server(self.studio, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        base = f'http://127.0.0.1:{server.server_port}'
        try:
            for headers in ({}, {'X-Studio-Token': self.studio.token, 'Origin': 'https://example.com'}):
                with self.assertRaises(HTTPError) as error:
                    urlopen(Request(base+'/api/config', data=b'{}', headers=headers))
                self.assertEqual(error.exception.code, 403)
            req = Request(base+'/api/config', data=b'{}', headers={'X-Studio-Token': self.studio.token})
            self.assertEqual(urlopen(req).status, 200)
        finally:
            server.shutdown(); server.server_close(); thread.join()
    def test_duplicate_job_refused(self):
        self.studio.job['status'] = 'running'
        with self.assertRaises(ValueError): self.studio.start_job('duplicate', lambda: None)


class PlanRegressionTest(unittest.TestCase):
    def test_portable_path_and_llm_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            plans = build_profiles(Hardware(8, 7, 16, 200, 'GPU'), {}, tmp)
            p = next(p for p in plans if p.key == 'starter')
            self.assertNotIn('D:/models', json.dumps(p.to_dict()))
            llm = next(d for d in p.downloads if d.kind == 'llm')
            self.assertEqual(p.config_patch['models']['llm']['default_model'], llm.repo)
            self.assertEqual(llm.size_gb, 2)
            self.assertTrue(p.quant_hints)
    def test_standard_contains_mesh_and_dependencies(self):
        p = next(p for p in build_profiles(Hardware(32,30,128,200,'GPU'), {}) if p.key == 'standard')
        self.assertTrue(any(d.kind == 'mesh' for d in p.downloads))
        self.assertTrue(any(d.plugin_id == 'pydeps' for d in p.downloads))
    def test_disable_fp8_is_respected(self):
        with patch('backend.backends.quant._bnb_available', return_value=False), patch('backend.backends.quant._fp8_available', return_value=True):
            self.assertEqual(choose_quant('auto',12,10,allow_fp8=False)['quant'], 'none')
    def test_unknown_size_respects_bnb_opt_out(self):
        with patch('backend.backends.quant._bnb_available', return_value=True):
            self.assertEqual(choose_quant('auto',0,10,allow_bnb=False)['quant'], 'none')
    def test_ram_also_gates_gpu_fit(self):
        self.assertFalse(recommend_quant(12,40,8,kind='image')['fits'])
    def test_fp8_reaches_actual_model_casting(self):
        import sys
        from backend.backends.direct import DirectBackend
        component = Mock()
        pipe = Mock(transformer=component)
        loader = Mock(); loader.from_pretrained.return_value = pipe
        fake_diffusers = SimpleNamespace(TestPipeline=loader)
        fake_torch = SimpleNamespace(bfloat16='bf16', float8_e4m3fn='fp8')
        backend = DirectBackend.__new__(DirectBackend); backend._log = Mock()
        with patch.dict(sys.modules, {'diffusers': fake_diffusers, 'torch': fake_torch}), patch('backend.backends.quant.estimate_model_gb', return_value=1), patch('backend.backends.quant.probe_vram', return_value=(32,30,'GPU')), patch('backend.backends.quant.choose_quant', return_value={'quant':'fp8','offload':'','why':'test'}):
            result = backend._load_pipe('TestPipeline', 'local', quant_pref='fp8')
        self.assertIs(result, pipe)
        component.enable_layerwise_casting.assert_called_once_with(storage_dtype='fp8', compute_dtype='bf16')
        pipe.to.assert_called_once_with('cuda')
