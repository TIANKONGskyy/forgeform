# -*- coding: utf-8 -*-
import json

f = r"D:\ComfyUI-New\blueprints\Image to Model (Hunyuan3d 2.1).json"
d = json.load(open(f, encoding="utf-8"))
print("顶层 keys:", list(d.keys()))

# 递归找所有 subgraph 的节点
def walk(obj, depth=0):
    if isinstance(obj, dict):
        if "nodes" in obj and isinstance(obj["nodes"], list):
            for n in obj["nodes"]:
                t = n.get("type", "?")
                wv = n.get("widgets_values", [])
                print(f"{'  '*depth}{t}  {wv if wv else ''}")
        for v in obj.values():
            walk(v, depth+1)
    elif isinstance(obj, list):
        for v in obj:
            walk(v, depth)

walk(d)
