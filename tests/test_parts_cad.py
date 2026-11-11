"""把 parts_cad 的验证接进标准入口（pytest），否则它只存在于我的一次性 shell 命令里。

设计取舍：
  · cadquery 2.8.0 只装在 D:\\comfy312，仓库 pytest 跑在 miniconda（没 cadquery）
    → 用子进程调 comfy312 的解释器，缺它就 **skip**（不是 fail）✓
  · 原件是用户桌面上的 SolidWorks 导出件，属外部资源 → 不在就 skip ✓
  · 复现件是产物（.gitignore 掉了）→ 测试先重新生成，再比对 ✓（顺带验了 gen_part1.py 本身）
"""
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PARTS = ROOT / "parts_cad"
import os

# 外部资源：装了 CadQuery 的解释器 + 用户自己的原件 STL。
# 用环境变量给，没给就走不存在的占位路径 → 下面的 skipUnless 自动跳过。
CAD_PY = Path(os.environ["CAD_PYTHON"]) if os.environ.get("CAD_PYTHON") else Path("__unset__")
ORIGINAL = Path(os.environ["PART1_ORIGINAL"]) if os.environ.get("PART1_ORIGINAL") else Path("__unset__")


@unittest.skipUnless(CAD_PY.exists(), "comfy312 解释器不在（cadquery 只装在那里）")
@unittest.skipUnless(ORIGINAL.exists(), "桌面原件 零件1.STL 不在，无法对比")
class TestPart1RoundTrip(unittest.TestCase):
    def test_rebuild_then_verify_matches_original(self):
        # 只去掉 PYTHONPATH（AGENTS.md 铁律：不清就会被 Hermes venv 的包污染），
        # 其余**必须继承** —— 我第一版传了个极简 env，连 HOME 都没有，
        # cadquery 直接 RuntimeError: Could not determine home directory ✗
        import os
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        gen = subprocess.run([str(CAD_PY), str(PARTS / "gen_part1.py")],
                             cwd=str(ROOT), capture_output=True, text=True,
                             encoding="utf-8", errors="ignore", timeout=300, env=env)
        self.assertEqual(gen.returncode, 0, f"生成失败：{gen.stdout[-400:]} {gen.stderr[-400:]}")
        self.assertIn("85.00 × 44.00 × 6.00", gen.stdout, "包围盒必须是 85×44×6")

        ver = subprocess.run([str(CAD_PY), str(PARTS / "verify_part1.py")],
                             cwd=str(ROOT), capture_output=True, text=True,
                             encoding="utf-8", errors="ignore", timeout=300, env=env)
        self.assertEqual(ver.returncode, 0, f"逐特征比对未通过：{ver.stdout[-600:]}")
        self.assertIn("4/4 passed", ver.stdout, ver.stdout[-300:])


if __name__ == "__main__":
    unittest.main()
