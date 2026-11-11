# -*- coding: utf-8 -*-
"""copy_compare.py - 整理 cfg=3.5(旧) vs cfg=1.0(新) 画质对照图到桌面"""
import csv, shutil
from pathlib import Path

CSV = Path(r"D:\AI-Workshop\outputs\continuous\generation_log.csv")
DESK = Path(r"C:\Users\mac\Desktop\AI创意工坊_优化工程\画质对照")
OLD_DIR = DESK / "旧_cfg3.5_过饱和"
NEW_DIR = DESK / "新_cfg1.0_修复后"
OLD_DIR.mkdir(parents=True, exist_ok=True)
NEW_DIR.mkdir(parents=True, exist_ok=True)

# cfg 修复 commit 是 d189544；之前是 cfg=3.5，之后（含 d189544）是 cfg=1.0
OLD_GITS = {"0d41f2a", "ccf4402", "3a588d0"}   # cfg=3.5 时代
NEW_GITS = {"0b62079", "b423359", "f815a3d"}   # cfg=1.0 时代

# 8 条 prompt 池 → 短名
PROMPT_SHORT = {
    "a red ceramic teapot with gold trim, studio product photography": "01_红陶茶壶",
    "a small robot toy on white background, soft studio lighting": "02_机器人玩具",
    "a wooden sailing ship model, intricate rigging, museum display": "03_木帆船",
    "a glass chess set, dramatic rim lighting, dark background": "04_玻璃国际象棋",
    "a brass pocket watch with visible gears, macro photography": "05_黄铜怀表",
    "a miniature stone castle with moss, fantasy diorama": "06_石城堡",
    "a chrome sphere on reflective surface, abstract minimalism": "07_铬球",
    "a knitted wool scarf, cozy warm tones, flat lay": "08_针织围巾",
}

old_seen = {}
new_seen = {}
with open(CSV, encoding="utf-8") as f:
    for row in csv.DictReader(f):
        git = row.get("git", "")
        prompt = row.get("prompt", "")
        path = row.get("path", "")
        if not path or not Path(path).exists():
            continue
        short = None
        for k, v in PROMPT_SHORT.items():
            if prompt.startswith(k.split(",")[0][:20]):
                short = v
                break
        if not short:
            continue
        if git in OLD_GITS and short not in old_seen:
            old_seen[short] = path
        elif git in NEW_GITS and short not in new_seen:
            new_seen[short] = path

# 复制（同 prompt 配对）
paired = 0
for short in PROMPT_SHORT.values():
    op, np_ = old_seen.get(short), new_seen.get(short)
    if op and np_:
        shutil.copy2(op, OLD_DIR / f"{short}_旧cfg3.5.png")
        shutil.copy2(np_, NEW_DIR / f"{short}_新cfg1.0.png")
        paired += 1

# 写说明
readme = DESK / "说明.txt"
readme.write_text(
    "画质对照说明（天枢 2026-08-14）\n\n"
    f"成功配对 {paired}/8 组（同 prompt，但 seed 不同，构图会略有差异）。\n\n"
    "重点看【色彩方向】，不是像素级对比：\n"
    "- 旧_cfg3.5：KSampler cfg=3.5（错误用法，每步跑两次前向 + 经典 CFG 过饱和伪影）\n"
    "- 新_cfg1.0：KSampler cfg=1.0 + CLIPTextEncodeFlux(guidance=3.5)（FLUX 官方正确用法）\n\n"
    "请判断：新图是否【过饱和消失 / 色彩更自然 / 细节更干净】？\n"
    "若新图更好 → 收尾打 tag；若不如旧图 → 我回退 cfg 到 3.5。\n",
    encoding="utf-8",
)
print(f"配对 {paired} 组，已复制到 {DESK}")
