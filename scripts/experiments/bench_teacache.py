"""TeaCache 加速对比测试：1024×1024 标准质量，开 vs 关"""
import sys, time
sys.path.insert(0, "D:/AI-Workshop")
from backend.pipeline import Pipeline

pipe = Pipeline("D:/AI-Workshop/config.json")

def run(tag, seed):
    t0 = time.time()
    r = pipe.generate_images(
        prompt="a red sports car in a studio, product photography, high detail",
        count=1, model_key="flux", quality_name="标准",
        resolution="1024×1024 (正方形)", seed=seed, negative="",
    )
    dt = time.time() - t0
    ok = r[0].success if r else False
    print(f"[{tag}] 耗时 {dt:.1f}s  success={ok}")
    return dt

# 1. TeaCache 开（config 默认 true）
pipe.config["models"]["image"]["flux"]["teacache"] = True
t_on = run("TeaCache ON ", 111111)

# 2. TeaCache 关
pipe.config["models"]["image"]["flux"]["teacache"] = False
t_off = run("TeaCache OFF", 222222)

print(f"\n=== 加速比: {t_off/t_on:.2f}x (开 {t_on:.1f}s vs 关 {t_off:.1f}s) ===")
