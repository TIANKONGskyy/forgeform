"""TeaCache 接入端到端冒烟测试（512 小图，验证工作流不炸 + 出图）"""
import sys, time
sys.path.insert(0, "D:/AI-Workshop")
from backend.pipeline import Pipeline

pipe = Pipeline("D:/AI-Workshop/config.json")

print("ComfyUI 运行:", pipe.is_comfy_running())
t0 = time.time()
results = pipe.generate_images(
    prompt="a small red robot toy on white background",
    count=1, model_key="flux", quality_name="快速",
    resolution="512×512 (快速)", seed=424242, negative="",
)
dt = time.time() - t0
print(f"耗时 {dt:.1f}s")
for r in results:
    print("success:", r.success, "| path:", r.output_path, "| meta:", r.metadata)

# 再跑一次相同参数，验证缓存命中（应秒回）
t1 = time.time()
r2 = pipe.generate_images(
    prompt="a small red robot toy on white background",
    count=1, model_key="flux", quality_name="快速",
    resolution="512×512 (快速)", seed=424242, negative="",
)
dt2 = time.time() - t1
print(f"第二次(缓存)耗时 {dt2:.1f}s, cached={r2[0].metadata.get('cached') if r2 else None}")
