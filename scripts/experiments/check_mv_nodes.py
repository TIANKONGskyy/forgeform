# -*- coding: utf-8 -*-
import urllib.request, json
oi = json.load(urllib.request.urlopen('http://127.0.0.1:8188/object_info', timeout=15))
for name in ['GetImagesFromBatchIndexed', '[Comfy3D] Multi Background Remover', '[Comfy3D] Hunyuan3D V2 ShapeGen MV', '[Comfy3D] Save 3D Mesh']:
    if name in oi:
        n = oi[name]
        print(f"=== {name} ===")
        print("输入:", json.dumps(n["input"], ensure_ascii=False)[:700])
        print("输出:", n.get("output"))
        print()
