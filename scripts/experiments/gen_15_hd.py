# -*- coding: utf-8 -*-
"""生成 15 张 1024×1024 高质量图（提示词扩写详细描述）

流程：
1. 15 个基础题材 → Ollama qwen3:4b 扩写成详细英文描述（材质/光照/构图/细节）
2. 1024×1024 标准质量生成（TeaCache 加速）
3. 输出到桌面新文件夹 AI生成图像_1024/
4. 温度保护：每张间隔 + >82°C 暂停

用法: python gen_15_hd.py
"""
import sys, os, time, subprocess
from pathlib import Path

sys.path.insert(0, "D:/AI-Workshop")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

from PIL import Image

WORKSPACE = Path("D:/AI-Workshop")
OUT_DIR = Path("C:/Users/mac/Desktop") / "AI生成图像_1024"

# 15 个基础题材（比原来的 8 个更多样）
BASE_PROMPTS = [
    "a red ceramic teapot with gold trim",
    "a small robot toy on white background",
    "a wooden sailing ship model with intricate rigging",
    "a glass chess set with dramatic lighting",
    "a brass pocket watch with visible gears",
    "a miniature stone castle with moss",
    "a chrome sphere on reflective surface",
    "a knitted wool scarf in warm tones",
    "a vintage leather camera on wooden desk",
    "a crystal dragon figurine",
    "a steampunk owl sculpture with gears",
    "a jade chinese tea cup set",
    "a silver ring with sapphire gemstone",
    "a hand-carved wooden bear statue",
    "a glass perfume bottle with golden liquid",
]


def gpu_temp() -> int:
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=temperature.gpu",
             "--format=csv,noheader,nounits"],
            stderr=subprocess.DEVNULL, timeout=5).decode().strip()
        return int(out.split("\n")[0].strip())
    except Exception:
        return 60


def enhance(prompt: str) -> str:
    """用 Ollama 扩写详细描述，返回 IMAGE 段"""
    from backend.prompt_enhancer import enhance as llm_enhance, split_sections
    raw = llm_enhance(prompt, timeout=120)
    sections = split_sections(raw)
    img = sections.get("image", "").strip()
    if img and len(img) > 20:
        return img
    # 扩写失败回退到原文
    return prompt


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    from backend.pipeline import Pipeline
    pipe = Pipeline("D:/AI-Workshop/config.json")

    print(f"=== 生成 15 张 1024×1024 高清图 ===")
    print(f"输出目录: {OUT_DIR}")

    ok = 0
    for i, base in enumerate(BASE_PROMPTS, 1):
        # 温度保护：>82°C 暂停等降温
        while gpu_temp() >= 82:
            print(f"  ⏳ GPU {gpu_temp()}°C 偏高，等待降温...")
            time.sleep(20)

        # 扩写详细描述
        print(f"[{i}/15] 扩写: {base}")
        detailed = enhance(base)
        print(f"         → {detailed[:80]}...")

        # 1024×1024 标准质量生成
        t0 = time.time()
        results = pipe.generate_images(
            prompt=detailed, count=1, model_key="flux",
            quality_name="标准", resolution="1024×1024 (正方形)",
            seed=10000 + i, negative="", skip_cache=True,
        )
        dt = time.time() - t0

        if results and results[0].success:
            src = Path(results[0].output_path)
            dst = OUT_DIR / f"{i:02d}_{base.replace(' ', '_')[:40]}.png"
            # 用 PIL 确保 1024 分辨率（若生成器已 1024 则直接复制）
            try:
                im = Image.open(src)
                if im.size != (1024, 1024):
                    im = im.resize((1024, 1024), Image.LANCZOS)
                im.save(dst)
            except Exception:
                import shutil
                shutil.copy2(src, dst)
            ok += 1
            print(f"  ✅ {i:02d} {dt:.1f}s → {dst.name} (GPU {gpu_temp()}°C)")
        else:
            err = results[0].error if results else "no result"
            print(f"  ❌ {i:02d} 失败: {str(err)[:60]}")

        # 每张间隔降温
        time.sleep(4)

    print(f"\n✅ 完成 {ok}/15 张，输出目录: {OUT_DIR}")


if __name__ == "__main__":
    main()
