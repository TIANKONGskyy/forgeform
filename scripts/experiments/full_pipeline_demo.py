# -*- coding: utf-8 -*-
"""full_pipeline_demo.py - 全流程展示：海盗船归档 + 3 个主题全流程跑，产物放桌面"""
import sys, time, shutil, glob, os
from pathlib import Path
sys.path.insert(0, "D:/AI-Workshop")
from backend.pipeline import Pipeline

pipe = Pipeline("D:/AI-Workshop/config.json")
DESK = Path(r"C:\Users\mac\Desktop\AI创意工坊_全流程展示")
DESK.mkdir(parents=True, exist_ok=True)

def copy_latest(patterns, dest_dir, new_name):
    """从 outputs 找最新匹配文件复制到 dest_dir"""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    for pat, name in patterns:
        files = sorted(glob.glob(pat), key=os.path.getmtime, reverse=True)
        if files:
            shutil.copy2(files[0], dest_dir / name)
            print(f"  ✓ {name} <- {Path(files[0]).name}")
            return str(dest_dir / name)
    return ""

def run_topic(topic_name, prompt_text, folder):
    """跑一个主题的全流程"""
    print(f"\n{'='*50}\n主题: {topic_name}\n{'='*50}")
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)

    # 1. 扩写
    print("  [1/4] 扩写...")
    sections = pipe.enhance_sections(prompt_text)
    img_prompt = sections["image"]
    td_prompt = sections["three_d"]
    (folder / "prompt.txt").write_text(
        f"原始提示: {prompt_text}\n\n【IMAGE】\n{img_prompt}\n\n【3D】\n{td_prompt}",
        encoding="utf-8")
    print(f"    IMAGE {len(img_prompt)}字 / 3D {len(td_prompt)}字")

    # 2. 图片
    print("  [2/4] 图片生成...")
    t0 = time.time()
    results = pipe.generate_images(prompt=img_prompt, count=1, model_key="flux",
                                   quality_name="高质量", resolution="1024×1024 (正方形)",
                                   seed=-1, negative="", skip_cache=True)
    el = time.time() - t0
    img = results[0].output_path if results and results[0].success else ""
    if img:
        shutil.copy2(img, folder / "图片.png")
        print(f"    {el:.0f}s ✓ 图片.png")
    else:
        print(f"    {el:.0f}s ❌ 图片失败"); return

    # 3. 3D
    print("  [3/4] 3D 建模（高质量 octree 512）...")
    t0 = time.time()
    td = pipe.generate_3d(model_key="hunyuan3d", quality_name="高质量", image_path=img)
    el = time.time() - t0
    glb = td.output_path if td.success else ""
    if glb:
        shutil.copy2(glb, folder / "模型.glb")
        print(f"    {el:.0f}s ✓ 模型.glb")
    else:
        print(f"    {el:.0f}s ❌ 3D失败: {td.error}"); return

    # 4. STL
    print("  [4/4] STL 转换（high_quality 15万面）...")
    t0 = time.time()
    stl = pipe.export_stl(glb, target_size_mm=120, optimize_preset="high_quality")
    el = time.time() - t0
    if stl and stl.success:
        shutil.copy2(stl.output_path, folder / "打印模型.stl")
        try:
            import trimesh
            m = trimesh.load(stl.output_path, force="mesh")
            print(f"    {el:.0f}s ✓ 打印模型.stl（{len(m.faces)} 面，水密={m.is_watertight}）")
        except Exception:
            print(f"    {el:.0f}s ✓ 打印模型.stl")
    else:
        print(f"    ❌ STL失败: {stl.error if stl else '?'}")

# ── 0. 海盗船归档 ──
print("=== 海盗船归档（刚才跑的）===")
pirate = DESK / "海盗船"
pirate.mkdir(parents=True, exist_ok=True)
# 找最新产物（最近一次 end_to_end_test 生成的）
img_files = sorted(glob.glob("D:/AI-Workshop/outputs/images/img_*.png"), key=os.path.getmtime, reverse=True)
glb_files = sorted(glob.glob("D:/AI-Workshop/outputs/3d/model_*.glb"), key=os.path.getmtime, reverse=True)
stl_files = sorted(glob.glob("D:/AI-Workshop/outputs/stl/*_optimized.stl"), key=os.path.getmtime, reverse=True)
if img_files: shutil.copy2(img_files[0], pirate / "图片.png")
if glb_files: shutil.copy2(glb_files[0], pirate / "模型.glb")
if stl_files: shutil.copy2(stl_files[0], pirate / "打印模型.stl")
# 扩写文本（重新生成一次，或从上次读取）
sections = pipe.enhance_sections("海盗船，船上有骷髅头装饰、舰炮、桅杆、绳索、木纹甲板")
(pirate / "prompt.txt").write_text(
    f"原始提示: 海盗船，船上有骷髅头装饰、舰炮、桅杆、绳索、木纹甲板\n\n【IMAGE】\n{sections['image']}\n\n【3D】\n{sections['three_d']}",
    encoding="utf-8")
print(f"  海盗船归档完成: 图片/模型/STL/prompt 共 {len(list(pirate.glob('*')))} 个文件")

# ── 三个主题 ──
topics = [
    ("中国龙", "一条中国龙，鳞片浮雕、龙须、利爪、祥云底座，东方神话风格"),
    ("蒸汽朋克机械章鱼", "蒸汽朋克机械章鱼，黄铜齿轮、铜管、铆钉、触须吸盘、玻璃穹顶头部"),
    ("哥特式大教堂", "哥特式大教堂，尖塔、飞扶壁、玫瑰窗、石雕门环、滴水兽"),
]
for name, prompt in topics:
    run_topic(name, prompt, DESK / name)

print(f"\n=== 全部完成，产物在 {DESK} ===")
