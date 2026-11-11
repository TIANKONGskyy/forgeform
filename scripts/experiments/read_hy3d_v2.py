# -*- coding: utf-8 -*-
import json

f = r"D:\ComfyUI-New\custom_nodes\ComfyUI-3D-Pack-main\example_workflows\Hunyuan3D_V2\Hunyuan3D_V2.json"
d = json.load(open(f, encoding="utf-8"))

print("=== Hunyuan3D_V2.json 节点链路 ===")
for n in d["nodes"]:
    t = n.get("type", "?")
    wv = n.get("widgets_values", [])
    print(f"  {t}  {wv if wv else ''}")

print("\n=== links（连接关系）===")
for l in d.get("links", []):
    print(f"  [{l[0]}] {l[1]}->{l[3]} ({l[5]})")
