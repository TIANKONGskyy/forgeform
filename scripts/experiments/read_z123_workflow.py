# -*- coding: utf-8 -*-
import json

f = r"D:\ComfyUI-New\custom_nodes\ComfyUI-3D-Pack-main\example_workflows\Zero123Plus_to_InstantMesh.json"
d = json.load(open(f, encoding="utf-8"))

print("=== 节点类型链路 ===")
for n in d["nodes"]:
    t = n.get("type", "?")
    title = n.get("title", "")
    print(f"  {t}  {title if title else ''}")

print("\n=== Load Diffusers Pipeline 的 widgets_values ===")
for n in d["nodes"]:
    if "Load Diffusers" in n.get("type", ""):
        wv = n.get("widgets_values", [])
        print(f"  {n['type']}: {wv}")
