import tempfile,unittest
from pathlib import Path
from unittest.mock import Mock,patch
from types import SimpleNamespace
from backend.conversation import ConversationManager
from backend.comfy_client import ComfyUIClient

class ReferenceMeshTest(unittest.TestCase):
 def test_invalid_fallback_never_becomes_success(self):
  from backend.pipeline import Pipeline
  with tempfile.TemporaryDirectory() as temp:
   import trimesh
   bad=Path(temp)/'slab.glb'
   trimesh.creation.box(extents=(100,100,0.5)).subdivide_to_size(max_edge=2).export(bad)
   pipe=Pipeline()
   with patch.object(pipe,'generate_3d',return_value=SimpleNamespace(success=True,output_path=str(bad))) as retry:
    result=pipe._finalize_3d(str(bad),'hunyuan21','高质量','ref.png',False)
   self.assertFalse(result.success);self.assertFalse(result.output_path)
   self.assertIn('质量检查未通过',result.error);self.assertEqual(retry.call_count,1)
 def test_uploaded_reference_is_not_silently_repainted(self):
  with tempfile.TemporaryDirectory() as temp:
   ref=Path(temp)/'original.png';ref.write_bytes(b'input-image')
   pipeline=Mock()
   state={'selected':str(ref),'ref_upload':str(ref)}
   result=ConversationManager.__new__(ConversationManager)._enhance_selected(state,pipeline)
   self.assertEqual(result,str(ref));pipeline.enhance_image.assert_not_called()
   self.assertEqual(ref.read_bytes(),b'input-image')
 def test_foreground_alpha_reaches_shape_generation(self):
  graph=ComfyUIClient({})._build_hunyuan3d_workflow({'input_image':'original.png','texgen':False,'seed':1})['prompt']
  shape=next(n for n in graph.values() if n['class_type']=='[Comfy3D] Hunyuan3D 21 ShapeGen')
  join=graph[shape['inputs']['image'][0]]
  self.assertEqual(join['class_type'],'JoinImageWithAlpha')
  mask=graph[join['inputs']['alpha'][0]]
  self.assertEqual(mask['class_type'],'InvertMask')
  mask=graph[mask['inputs']['mask'][0]]
  self.assertEqual(mask['class_type'],'RemoveBackground')
  self.assertEqual(graph[mask['inputs']['image'][0]]['class_type'],'LoadImage')
  self.assertFalse(shape['inputs']['remove_background'])

 def test_batch_job_uses_own_project_and_keeps_models_lazy(self):
  from backend import project_manager as pm
  import trimesh
  with tempfile.TemporaryDirectory() as temp, patch.object(pm,'PROJECTS_DIR',Path(temp)):
   other=pm.create_project();state=pm.create_project()
   ref=Path(state['path'])/'candidates'/'ref.png';ref.write_bytes(b'reference')
   state.update(selected=str(ref),ref_upload=str(ref))
   glb=Path(temp)/'source.glb';trimesh.creation.icosphere(subdivisions=1).export(glb)
   stl=Path(temp)/'source.stl';trimesh.creation.icosphere(subdivisions=1).export(stl)
   pipe=Mock(config={'stages':{'3d':{'default_model':'hunyuan21'}}})
   pipe.generate_3d.return_value=SimpleNamespace(success=True,output_path=str(glb))
   pipe.export_stl.return_value=SimpleNamespace(success=True,output_path=str(stl))
   untouched=(Path(other['path'])/'meta.json').read_bytes()
   with patch.object(ConversationManager,'_init_current_project') as init, patch('backend.pipeline.Pipeline',return_value=pipe), patch('backend.conversation._free_tuned_for_gpu_work'), patch('backend.conversation._rewarm_tuned_async') as warm_chat, patch('backend.conversation._warm_image_model_async') as warm_image, patch('backend.render3d.render_mesh_preview',return_value=''):
    manager=ConversationManager(project=state,interactive=False)
    result=manager._do_generate_3d(state)
   init.assert_not_called();warm_chat.assert_not_called();warm_image.assert_not_called()
   pipe.enhance_image.assert_not_called()
   self.assertIs(manager.current_project,state)
   self.assertTrue(Path(result['glb']).is_file());self.assertTrue(Path(state['stl_path']).is_file())
   self.assertEqual((Path(other['path'])/'meta.json').read_bytes(),untouched)

if __name__=='__main__':unittest.main()
