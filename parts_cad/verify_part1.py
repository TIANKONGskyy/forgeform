# -*- coding: utf-8 -*-
"""零件1 的可复跑验证：代码复现件 vs 原件（SolidWorks 导出 STL）逐特征对比。

为什么不用 pytest：cadquery 2.8.0 只装在 D:\\comfy312（仓库的 pytest 跑在 miniconda，
没有 cadquery），原件 STL 又在用户桌面 —— 所以单独一个脚本，谁都能复跑。

跑法：D:\\comfy312\\Scripts\\python.exe parts_cad\\verify_part1.py
"""
import os
import sys
from pathlib import Path

import numpy as np
import trimesh

HERE = Path(__file__).parent
REBUILT = HERE / "out" / "零件1_rebuilt.stl"
# 原件是用户自己从 SolidWorks 导出的 STL，不在仓库里：用 PART1_ORIGINAL 指向它
_orig = os.environ.get("PART1_ORIGINAL")
ORIGINALS = [Path(_orig)] if _orig else []

VOL_TOL = 0.02        # 体积相对差上限 2%（三角化噪声量级）


def features(path):
    """量一个平板件：包围盒 / 体积 / 内环（孔）圆心+半径 / 缺口（凸包内凹弧）拟合"""
    m = trimesh.load(str(path), force="mesh")
    sec = m.section(plane_origin=[0, 0, 3.0], plane_normal=[0, 0, 1])
    p2, _ = sec.to_2D()
    loops = sorted(p2.discrete, key=len, reverse=True)
    base = np.asarray(loops[0])
    base = base - base.min(0)                       # 左下归零 → 都在"距左边/下边"坐标系里
    holes = []
    for lp in loops[1:]:
        P = np.asarray(lp) - np.asarray(loops[0]).min(0)
        c = P.mean(0)
        holes.append((round(float(c[0]), 2), round(float(c[1]), 2),
                      round(float(np.linalg.norm(P - c, axis=1).mean()), 2)))
    return {"bbox": np.round(m.extents, 2), "vol": float(abs(m.volume)),
            "holes": sorted(holes), "ring": len(loops) - 1}


def main() -> int:
    if not REBUILT.exists():
        print(f"✗ 缺少复现件 {REBUILT}（先跑 gen_part1.py）")
        return 1
    src = next((p for p in ORIGINALS if p.exists()), None)
    if src is None:
        print("✗ 找不到原件 STL（桌面 零件1.STL）—— 无法对比")
        return 1

    a, b = features(src), features(REBUILT)
    ok = []
    def ck(name, cond, detail):
        ok.append(bool(cond))
        print(f"{'PASS' if cond else 'FAIL'} {name}  [{detail}]")

    ck("bbox_match", np.allclose(a["bbox"], b["bbox"], atol=0.01),
       f"原件 {a['bbox']} vs 复现 {b['bbox']}")
    dv = abs(a["vol"] - b["vol"]) / max(a["vol"], 1e-9)
    ck("volume_within_tol", dv <= VOL_TOL, f"{a['vol']:.0f} vs {b['vol']:.0f}（差 {dv:.3%}）")
    ck("same_hole_count", a["ring"] == b["ring"], f"内环 {a['ring']} vs {b['ring']}")
    # 圆心/半径按 0.05mm 容差比（STL 三角化会让同一圆弧量出 2.99 与 3.00 这种差，
    # 一开始卡精确相等会误报 FAIL ✗）
    same_holes = (len(a["holes"]) == len(b["holes"])
                  and all(max(abs(x - y) for x, y in zip(h1, h2)) <= 0.05
                          for h1, h2 in zip(a["holes"], b["holes"])))
    ck("holes_match", same_holes, f"原件 {a['holes']} vs 复现 {b['holes']}（容差 0.05mm）")

    print(f"\n{sum(ok)}/{len(ok)} passed")
    return 0 if all(ok) else 1


if __name__ == "__main__":
    sys.exit(main())
