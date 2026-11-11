# -*- coding: utf-8 -*-
"""Benchmark 反复比对工具（仿生进化：改前 vs 改后）

用固定 (prompt, seed) 集生成，对比：
1. phash 汉明距离（0=完全相同，越大差异越大）
2. 耗时变化
3. git commit 变化（确认改后确实用了新代码）

用法:
  python compare_benchmark.py                # 用内置 benchmark 集，跑一次并打印结果
  python compare_benchmark.py --save baseline.json   # 保存当前基线
  python compare_benchmark.py --compare baseline.json  # 与已保存基线对比
"""
import sys, os, json, time, argparse, subprocess
from pathlib import Path

sys.path.insert(0, "D:/AI-Workshop")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

WORKSPACE = "D:/AI-Workshop"
GEN_SCRIPT = Path(WORKSPACE) / "scripts" / "_gen_one.py"
PY = r"D:\miniconda3\python.exe"

# 固定 benchmark 集：(prompt, seed) —— seed 固定才能反复比对同输入
BENCHMARK = [
    ("a red ceramic teapot with gold trim, studio product photography", 55501),
    ("a small robot toy on white background, soft studio lighting", 55502),
    ("a glass chess set, dramatic rim lighting, dark background", 55503),
]


def hamming(a: str, b: str) -> int:
    if not a or not b or a.startswith("ERR") or b.startswith("ERR"):
        return -1
    try:
        return bin(int(a, 16) ^ int(b, 16)).count("1")
    except Exception:
        return -1


def run_one(prompt: str, seed: int) -> dict:
    r = subprocess.run(
        [PY, str(GEN_SCRIPT), "--prompt", prompt, "--seed", str(seed)],
        capture_output=True, text=True, timeout=300,
        encoding="utf-8", errors="replace", cwd=WORKSPACE,
    )
    for line in reversed(r.stdout.splitlines()):
        line = line.strip()
        if line.startswith("__GEN_RESULT__"):
            try:
                return json.loads(line[len("__GEN_RESULT__"):])
            except Exception as e:
                return {"success": False, "error": str(e)}
    return {"success": False, "error": "no result"}


def run_benchmark() -> dict:
    """跑完整 benchmark 集，返回 {key: result}"""
    results = {}
    for prompt, seed in BENCHMARK:
        key = f"{seed}:{prompt[:30]}"
        r = run_one(prompt, seed)
        results[key] = r
        print(f"  [{key}] {r.get('elapsed',-1)}s "
              f"phash={r.get('phash','')[:16]} git={r.get('git','?')} "
              f"{'✅' if r.get('success') else '❌'}")
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--save", type=str, help="保存结果到该 JSON 文件作为基线")
    ap.add_argument("--compare", type=str, help="与该 JSON 基线文件对比")
    args = ap.parse_args()

    print(f"=== Benchmark 集 ({len(BENCHMARK)} 项) ===")
    results = run_benchmark()

    if args.save:
        with open(args.save, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        print(f"\n✅ 基线已保存: {args.save}")

    if args.compare:
        with open(args.compare, "r", encoding="utf-8") as f:
            baseline = json.load(f)
        print(f"\n=== 对比基线 {args.compare} ===")
        print(f"{'项目':<38} {'phash距离':>8} {'耗时基线':>8} {'耗时现在':>8} {'Δ耗时':>8}")
        for key, b in baseline.items():
            cur = results.get(key, {})
            dist = hamming(b.get("phash", ""), cur.get("phash", ""))
            t_b = b.get("elapsed", -1)
            t_c = cur.get("elapsed", -1)
            dt = round(t_c - t_b, 1) if t_b > 0 and t_c > 0 else "-"
            g_b = b.get("git", "?")
            g_c = cur.get("git", "?")
            print(f"{key:<38} {dist:>8} {t_b:>8} {t_c:>8} {str(dt):>8}  [{g_b}→{g_c}]")
        print("\n说明：phash距离 0=完全相同（改动不影响输出），越大=输出变化越大")


if __name__ == "__main__":
    main()
