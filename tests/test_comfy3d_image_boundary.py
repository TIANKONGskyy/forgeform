"""Exercise the installed helper used by Hunyuan, before loading any model."""
import ast,unittest
from pathlib import Path
import numpy as np
from PIL import Image

class Comfy3DImageBoundaryTest(unittest.TestCase):
 def test_rgba_and_rgb_pixels_survive_tensor_to_pil(self):
  import torch
  import os
  base=os.environ.get('COMFYUI_DIR','')
  if not base:
   self.skipTest('COMFYUI_DIR not set')
  path=Path(base)/'custom_nodes'/'ComfyUI-3D-Pack-main'/'shared_utils'/'image_utils.py'
  if not path.exists():
   self.skipTest('ComfyUI-3D-Pack image_utils.py not installed')
  tree=ast.parse(path.read_text(encoding='utf-8'))
  function=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='torch_imgs_to_pils')
  namespace={'np':np,'Image':Image,'torch':torch}
  exec(compile(ast.Module(body=[function],type_ignores=[]),str(path),'exec'),namespace)
  pixels=np.array([[[255,0,0,255],[0,0,255,0]],[[0,255,0,128],[255,255,255,255]]],dtype=np.uint8)
  for channels,mode in [(4,'RGBA'),(3,'RGB')]:
   with self.subTest(channels=channels):
    expected=pixels[...,:channels]
    output=namespace['torch_imgs_to_pils'](torch.from_numpy(expected.astype(np.float32)/255))[0]
    self.assertEqual(output.mode,mode)
    np.testing.assert_array_equal(np.array(output),expected)

if __name__=='__main__':unittest.main()
