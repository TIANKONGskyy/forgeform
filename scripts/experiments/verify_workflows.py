# -*- coding: utf-8 -*-
"""verify_workflows.py - 验证视频/3D 各精度档工作流构建正确（不实际生成）"""
import sys
sys.path.insert(0, "D:/AI-Workshop")
from backend.pipeline import Pipeline

pipe = Pipeline("D:/AI-Workshop/config.json")

print("=== 视频工作流（各精度档）===")
vq_map = {
    "快速": {"steps": 4, "turbo": True},
    "标准": {"steps": 6, "turbo": True},
    "高质量": {"steps": 8, "turbo": False},
    "极致": {"steps": 10, "turbo": False},
}
for q, vq in vq_map.items():
    params = {
        "model_config": pipe.config["models"]["video"]["h3"],
        "input_image": "test.png", "prompt": "", "seed": -1,
        "steps": vq["steps"], "turbo_enabled": vq["turbo"],
    }
    wf = pipe.comfy.build_workflow("video", params)
    for nid, node in wf["prompt"].items():
        if node["class_type"] == "KSampler":
            inp = node["inputs"]
            print(f"  视频[{q}] steps={inp['steps']} scheduler={inp['scheduler']} cfg={inp['cfg']} sampler={inp['sampler_name']}")

print("=== 3D 工作流（各精度档）===")
tq_map = {
    "快速": {"octree_resolution": 256, "guidance_scale": 5.0},
    "标准": {"octree_resolution": 384, "guidance_scale": 5.0},
    "高质量": {"octree_resolution": 512, "guidance_scale": 6.0},
}
for q, tq in tq_map.items():
    params = {
        "model_config": pipe.config["models"]["3d"]["hunyuan3d"],
        "input_image": "test.png", "seed": -1, "texgen": False,
        "octree_resolution": tq["octree_resolution"], "guidance_scale": tq["guidance_scale"],
    }
    wf = pipe.comfy.build_workflow("3d", params)
    for nid, node in wf["prompt"].items():
        ct = node["class_type"]
        if "ShapeGen" in ct and "Load" not in ct and "Pipeline" not in ct:
            inp = node["inputs"]
            print(f"  3D[{q}] octree={inp['octree_resolution']} guidance={inp['guidance_scale']} steps={inp['steps']}")

print("=== 验证通过 ===")
