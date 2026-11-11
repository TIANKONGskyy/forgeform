# -*- coding: utf-8 -*-
import json

f = r"D:\ComfyUI-New\custom_nodes\ComfyUI-3D-Pack-main\example_workflows\Hunyuan3D_2_1\Hunyuan2.1-Shapegen.json"
d = json.load(open(f, encoding="utf-8"))
print("=== Hunyuan2.1-Shapegen 节点链路 ===")
for n in d.get("nodes", []):
    t = n.get("type", "?")
    wv = n.get("widgets_values", [])
    print(f"  {t}  {wv if wv else ''}")
