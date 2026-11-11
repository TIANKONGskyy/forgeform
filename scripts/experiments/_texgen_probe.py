"""实验：Hunyuan3D 2.1 的 TexGen（贴图）到底能不能跑。

背景（2026-10-15 查出来的）：
    pipeline.py:731 把 texgen **硬编码成 False**，注释写：
        「TexGen 需要 custom_rasterizer CUDA 扩展编译（缺 nvcc+MSVC），默认 ShapeGen-only」
    → 结果：建模产物**没有贴图**，出来是个白模。用户看到的"建模出问题"就是这个。

而实测发现：
    · 贴图节点其实是装着的：[Comfy3D] Load Hunyuan3D 21 TexGen Pipeline / Hunyuan3D 21 TexGen
    · 但 custom_rasterizer 在 custom_nodes 里**根本不存在**（也没有 .pyd/.so），且本机无 nvcc

所以关键问题是：**那句"需要 custom_rasterizer"到底是经验事实，还是当初的推测？**
如果是后者，直接把 texgen 打开就能拿到带贴图的模型 —— 不用装任何东西。

本脚本只做这一件事：按 pipeline.py 完全相同的调用方式建工作流，把 texgen 设成 True，
提交、等结果，然后把**实际发生的错误原文**原样打出来（不吞异常）。

跑法：python scripts/_texgen_probe.py
"""

import json
import os
import sys
import time
from pathlib import Path

# ★ 回归开关：装了新依赖之后，必须回头确认"原来能跑的 ShapeGen-only"还活着。
#   否则就是拆东墙补西墙 —— 贴图没弄成，反而把能出白模的路径也搞挂了。
TEXGEN = "--no-texgen" not in sys.argv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import backend.comfy_client as CC  # noqa: E402

CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))

# 用真实的那个件——用户刚做的"熊大"（有红马甲、有颜色，正好能看出贴图有没有上）
PROJECT = ROOT / "projects" / "2026-10-15_081331"
INPUT = PROJECT / "selected.jpg"
if not INPUT.exists():
    # 退路：任何一个 selected.jpg
    cands = sorted(ROOT.glob("projects/*/selected.jpg"), key=lambda p: p.stat().st_mtime, reverse=True)
    INPUT = cands[0] if cands else None
if not INPUT or not INPUT.exists():
    print("❌ 找不到输入图（selected.jpg）")
    sys.exit(1)

print("=" * 70)
print("Hunyuan3D 2.1 + TexGen 可行性实验")
print("=" * 70)
print(f"  输入图: {INPUT}  ({INPUT.stat().st_size:,} B)")

client = CC.ComfyUIClient(CONFIG)

# 1) 上传图（3d 阶段 → media 实例）
try:
    up = client.upload_image(str(INPUT), stage="3d")
    print(f"  上传 → {up}")
except Exception as e:
    print(f"❌ 上传失败: {type(e).__name__}: {e}")
    sys.exit(1)

# 2) 建工作流 —— texgen=True（这就是本次实验的全部变量）
out_path = str(ROOT / "outputs" / "3d" / f"texgen_probe_{int(time.time())}.glb")
params = {
    "model_config": CONFIG["models"]["3d"]["hunyuan21"],
    "input_image": up,
    "output_path": out_path,
    "seed": -1,
    "texgen": TEXGEN,          # ★ 唯一的改动（--no-texgen 则回到原来的 ShapeGen-only）
    "octree_resolution": 256,   # 实验用小一点，先验证"能不能跑通"，不追极致质量
    "guidance_scale": 5.0,
    "steps": 20,
}

wf = client.build_workflow("3d", params)
nodes = wf["prompt"]
print(f"\n  工作流节点数 = {len(nodes)}   （texgen=False 时是 4；有贴图应该在 7~8）")
for nid, node in nodes.items():
    print(f"    {nid}: {node['class_type']}")

print("\n提交...")
t0 = time.time()
try:
    pid = client.submit_workflow(wf, stage="3d")
    print(f"  prompt_id = {pid}   （{time.time()-t0:.1f}s）")
except Exception as e:
    print(f"❌ 提交就失败了：{type(e).__name__}: {e}")
    sys.exit(1)

print("等待（TexGen 8 视角 × 768，可能几分钟）...")
try:
    res = client.wait_for_completion(pid, timeout=1500)
except Exception as e:
    print(f"❌ 等待中出错：{type(e).__name__}: {e}")
    sys.exit(1)

print(f"\n  用时 {round(time.time()-t0)}s")
outs = (res or {}).get("outputs") or {}
print(f"  输出节点数 = {len(outs)}")

mesh = None
try:
    mesh = client.get_result_3d(outs)
except Exception as e:
    print(f"  取结果出错（继续看下面）: {e}")

print(f"  结果 mesh = {mesh}")

print()
print("=" * 70)
if mesh and Path(mesh).exists():
    sz = Path(mesh).stat().st_size
    print("✅ 成功 —— TexGen 能跑通，那句「需要 custom_rasterizer」是过时的推测")
    print(f"   产物 {mesh}  ({sz:,} B)")
    print("   ★ 下一步：和 ShapeGen-only 的 720KB 白模比大小。")
    print("     带贴图的 GLB 会明显更大（多了纹理图集）——若大小相近则贴图其实没上。")
else:
    print("❌ 没拿到 mesh —— 看下面的错误原文定位缺什么")
    print(json.dumps(res, ensure_ascii=False, default=str)[:1500])
