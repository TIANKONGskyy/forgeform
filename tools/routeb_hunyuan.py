"""路线 B 第一步：Hunyuan 生成蛇的静态外壳。"""
import sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend.pipeline import Pipeline

OUT = Path(r"D:\InnerStructLM\data\route_b")
OUT.mkdir(parents=True, exist_ok=True)

p = Pipeline()
t0 = time.time()
prompt = (
    "single articulated toy snake, full body, side view, green and cream, "
    "segmented smooth body, small head, no background, product photo, "
    "one object only"
)
imgs = p.generate_images(
    prompt, count=1, model_key="qwen21_ref", quality_name="标准",
    resolution="1024×1024", seed=20261022,
)
print("IMAGES", [(r.success, r.output_path, r.error) for r in imgs], flush=True)
img = next((r.output_path for r in imgs if r.success), "")
if not img:
    raise SystemExit("image failed")
res = p.generate_3d(
    model_key="hunyuan21", quality_name="快速",
    image_path=img, octree_override=256,
)
print("3D", res.success, res.output_path, res.error, f"{time.time()-t0:.0f}s", flush=True)
if res.success:
    import shutil
    dst = OUT / "hunyuan_shell.glb"
    shutil.copyfile(res.output_path, dst)
    print("SHELL", dst, dst.stat().st_size, flush=True)
