import json
import os
import tempfile
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
