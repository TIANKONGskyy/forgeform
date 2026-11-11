import importlib,tempfile,unittest,json
from pathlib import Path
from unittest.mock import patch
from backend import project_manager as pm

class ProjectSelectionTest(unittest.TestCase):
 def test_image_messages_keep_all_images_and_visible_reply(self):
  with tempfile.TemporaryDirectory() as temp,patch.object(pm,'PROJECTS_DIR',Path(temp)),patch('threading.Thread.start'):
   app=importlib.import_module('app')
   rows=app._chat_value([{'role':'user','content':'cup','ai_reply':'model completed','images':['first.png','second.png']}])
   self.assertEqual(rows[0],['cup',('first.png','model completed')])
   self.assertEqual(rows[1],[None,('second.png','')])
   self.assertEqual(rows[2],[None,'model completed'])
 def test_build_restores_saved_chat_and_stl_download(self):
  with tempfile.TemporaryDirectory() as temp,patch.object(pm,'PROJECTS_DIR',Path(temp)),patch('threading.Thread.start'):
   app=importlib.import_module('app')
   from backend.conversation import ConversationManager
   state=pm.create_project()
   stl=Path(state['path'])/'model.stl';stl.write_bytes(b'saved-stl')
   state.update(phase='stl',stl_path=str(stl),conversation=[{'role':'user','content':'cup','ai_reply':'saved model completed','images':[]}])
   with patch.object(app,'conv',ConversationManager(project=state)):
    blocks=app.build()
   chat=next(c for c in blocks.blocks.values() if getattr(c,'elem_id',None)=='chatbox')
   file=next(c for c in blocks.blocks.values() if getattr(c,'elem_id',None)=='stl_file')
   self.assertIn('saved model completed',str(chat.value))
   self.assertTrue(file.visible);self.assertIn('model.stl',str(file.value))
 def test_project_order_uses_creation_time_instead_of_folder_prefix(self):
  with tempfile.TemporaryDirectory() as temp,patch.object(pm,'PROJECTS_DIR',Path(temp)):
   for name,created in [('video-old',1),('2026-new',2)]:
    folder=Path(temp)/name;folder.mkdir()
    (folder/'meta.json').write_text(json.dumps({'created':created}),encoding='utf-8')
   self.assertEqual([p['folder'] for p in pm.list_projects()],['2026-new','video-old'])
 def test_new_project_stays_selected_when_video_folder_sorts_first(self):
  with tempfile.TemporaryDirectory() as temp,patch.object(pm,'PROJECTS_DIR',Path(temp)),patch('threading.Thread.start'):
   app=importlib.import_module('app')
   from backend.conversation import ConversationManager
   manager=ConversationManager()
   original=manager.current_project
   real=manager.list_projects
   def listing():return [{'folder':'video-legacy','user_prompt':'prior video','phase':'confirm'},*real()]
   with patch.object(app,'conv',manager),patch.object(manager,'list_projects',side_effect=listing):
    rows=list(app.on_btn_new(original))
    new_state=rows[-1][0]
    self.assertNotEqual(new_state['folder'],original['folder'])
    self.assertEqual(rows[-1][12]['value'],new_state['folder'])
    self.assertEqual(manager.current_project['folder'],new_state['folder'])

if __name__=='__main__':unittest.main()
