import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.comfy_client import ComfyUIClient
from backend.env import normalize_config_paths
from backend.studio import Studio
from backend.backends.recommend import Download, Profile


class ProjectSettingsTest(unittest.TestCase):
    def test_selected_models_and_quality_survive_reopening(self):
        from backend import project_manager as pm
        with tempfile.TemporaryDirectory() as tmp, patch.object(pm, 'PROJECTS_DIR', Path(tmp)):
            state = pm.create_project()
            state.update(quality='快速', image_model='z_image', model_3d='hunyuan21',
                         auto_params={'quality_3d': '快速'})
            pm.save_project(state)
            restored = pm.load_project(state['folder'])
            for key in ('quality', 'image_model', 'model_3d', 'auto_params'):
                self.assertEqual(restored[key], state[key])


class PathResolutionTest(unittest.TestCase):
    def test_unset_environment_resolves_both_comfy_paths(self):
        with patch.dict(os.environ, {}, clear=True), patch(
            'backend.backends.detect.guess_comfy_dir', return_value=str(Path('/models/comfy').resolve())
        ):
            cfg = {'paths': {'comfyui': '${COMFYUI_DIR}', 'models': '${COMFYUI_DIR}/models'}}
            normalize_config_paths(cfg)
        self.assertNotIn('${', json.dumps(cfg))
        self.assertEqual(Path(cfg['paths']['models']), Path(cfg['paths']['comfyui']) / 'models')

    def test_explicit_paths_are_preserved(self):
        cfg = {'paths': {'comfyui': str(Path('/custom/comfy').resolve()),
                         'models': str(Path('/shared/models').resolve())}}
        original = dict(cfg['paths'])
        normalize_config_paths(cfg)
        self.assertEqual(cfg['paths']['comfyui'], original['comfyui'])
        self.assertEqual(cfg['paths']['models'], original['models'])


class HunyuanWorkflowTest(unittest.TestCase):
    def test_alpha_survives_load_and_submission(self):
        client = ComfyUIClient({})
        workflow = client.build_workflow('3d', {
            'model_config': {'name': 'Hunyuan3D 2.1'},
            'input_image': 'transparent.png', 'remove_background_done': True,
            'max_facenum': 80000})
        graph = workflow['prompt']
        shape = graph['3']['inputs']
        self.assertEqual(graph[shape['image'][0]]['inputs']['alpha'], ['2', 1])
        self.assertFalse(shape['remove_background'])
        self.assertFalse(shape['auto_cleanup'])
        self.assertEqual(shape['max_facenum'], 80000)
        with patch('backend.comfy_client.requests.post') as post:
            post.return_value.json.return_value = {'prompt_id': 'qa'}
            self.assertEqual(client.submit_workflow(workflow, stage='3d'), 'qa')
            self.assertEqual(post.call_args.kwargs['json']['prompt'], graph)

    def test_filename_is_not_proof_of_transparency(self):
        workflow = ComfyUIClient({}).build_workflow('3d', {
            'model_config': {'name': 'Hunyuan3D 2.1'}, 'input_image': 'opaque_prep.png'})
        self.assertIn('RemoveBackground', [n['class_type'] for n in workflow['prompt'].values()])


class StlStageContractTest(unittest.TestCase):
    """export_stl 返回 StageResult，不是 (ok, path) 二元组。

    2026-11-16 实测：`POST /v1/generate` 带 "stl" 阶段时，模型已经建好、
    STL 也已经写盘，却在最后一步 `ok, stl_path = pipe.export_stl(...)` 抛
    TypeError → 任务被判 failed，调用方拿不到产物。calibrate.py 同样受影响，
    只是它把异常吞成「导出异常」。
    """

    def test_api_generate_finishes_with_stl_stage(self):
        import importlib.util
        from backend.stages.base import StageResult

        spec = importlib.util.spec_from_file_location(
            'ff_api_server', Path(__file__).resolve().parents[1] / 'tools' / 'api_server.py')
        api = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(api)

        with tempfile.TemporaryDirectory() as tmp:
            glb = Path(tmp) / 'model.glb'
            glb.write_bytes(b'glTF')
            stl = Path(tmp) / 'model.stl'
            stl.write_bytes(b'solid')

            class FakePipe:
                config = {'stages': {'image': {'default_model': 'qwen21_ref'},
                                     '3d': {'default_model': 'hunyuan_mini'}}}

                def generate_3d(self, **kwargs):
                    return StageResult(True, str(glb))

                def export_stl(self, path, size_mm):
                    return StageResult(True, str(stl), metadata={'faces': 10})

            job = {'id': 'job-test', 'created': time.time(), 'prompt': 'x',
                   'opts': {'stages': ['mesh', 'stl'], 'image_path': str(glb)},
                   'files': {}, 'status': 'queued', 'stage': '', 'percent': 0.0}
            api.JOBS['job-test'] = job
            self.addCleanup(api.JOBS.pop, 'job-test', None)
            with patch.object(api, 'pipeline', return_value=FakePipe()):
                api._run_job(job)

        self.assertEqual(job['status'], 'succeeded', job.get('error'))
        self.assertEqual(job['files']['stl'], str(stl))


class InstallFallbackTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        (root / 'config.json').write_text('{}')
        (root / '.venv').mkdir()
        (root / '.venv/.deps-ok').touch()
        self.studio = Studio(root)

    def test_proxy_failure_uses_direct_download(self):
        self.studio.plans['deps'] = Profile('deps', 'deps', '', 'direct',
            downloads=[Download('framework', 'pydeps', 'dependencies')])
        calls = []
        def command(argv):
            calls.append(argv)
            if any('gh-proxy.com' in arg for arg in argv):
                raise RuntimeError('proxy unavailable')
        with patch.object(self.studio, 'command', side_effect=command):
            self.studio.install('deps')
        self.assertTrue(any(any(arg.startswith('https://github.com/Tencent-Hunyuan/')
                                for arg in call) for call in calls))
