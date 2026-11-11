# -*- coding: utf-8 -*-
"""3D 线单任务生成器子进程（消费图像线的产物 → 生成 GLB）

用法: python _gen_3d.py --image <路径> [--model triposplat]
输出 __GEN_RESULT__ JSON
"""
import sys, os, json, time, argparse, subprocess
from pathlib import Path

sys.path.insert(0, "D:/AI-Workshop")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")


def git_head() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd="D:/AI-Workshop", stderr=subprocess.DEVNULL,
        ).decode().strip()
    except Exception:
        return "unknown"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", required=True)
    ap.add_argument("--model", default="triposplat")
    args = ap.parse_args()

    from backend.pipeline import Pipeline
    pipe = Pipeline("D:/AI-Workshop/config.json")

    t0 = time.time()
    r = pipe.generate_3d(model_key=args.model, image_path=args.image)
    elapsed = time.time() - t0

    out = {
        "success": bool(r and r.success),
        "path": r.output_path if r and r.success else "",
        "elapsed": round(elapsed, 1),
        "git": git_head(),
        "model": args.model,
    }
    if not out["success"]:
        out["error"] = (r.error if r else "no result")[:200]

    print("__GEN_RESULT__" + json.dumps(out, ensure_ascii=False))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
