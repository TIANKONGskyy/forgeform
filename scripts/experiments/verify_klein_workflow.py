# -*- coding: utf-8 -*-
"""verify_klein_workflow.py - 验证 Klein 工作流构建"""
import sys
sys.path.insert(0, "D:/AI-Workshop")
from backend.pipeline import Pipeline

pipe = Pipeline("D:/AI-Workshop/config.json")
cfg = pipe.config["models"]["image"]["flux_klein"]
params = {"model_config": cfg, "prompt": "a red teapot", "negative_prompt": "blurry",
          "width": 512, "height": 512, "steps": 8, "seed": 12345}
wf = pipe.comfy.build_workflow("image", params)
print("=== Klein 工作流节点 ===")
for nid, node in wf["prompt"].items():
    print(f"  {nid}: {node['class_type']}")
print("节点数:", len(wf["prompt"]))
