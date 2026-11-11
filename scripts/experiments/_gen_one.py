# -*- coding: utf-8 -*-
"""单图生成子进程（仿生热更新核心）

每次被守护循环 spawn 时，都会 import 磁盘上【最新】的 backend 代码。
这样修改 backend 后，下一次生成自动用新版，主循环无需重启。

用法:
  python _gen_one.py --prompt "..." --seed 42 [--resolution "512×512 (快速)"]
                    [--quality "快速"] [--model flux] [--negative "..."]

输出 JSON（stdout 最后一行）:
  {"success": true, "path": "...", "phash": "hex64", "elapsed": 9.5,
   "seed": 42, "git": "a65bc73", "resolution": "...", "quality": "..."}
"""
import sys, os, json, time, argparse, subprocess

sys.path.insert(0, "D:/AI-Workshop")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

from PIL import Image


def dhash(path: str, size: int = 8) -> str:
    """64-bit 感知哈希（自实现，不依赖 imagehash）"""
    img = Image.open(path).convert("L").resize((size + 1, size), Image.LANCZOS)
    px = list(img.getdata())
    bits = []
    for r in range(size):
        row = r * (size + 1)
        for c in range(size):
            bits.append("1" if px[row + c] > px[row + c + 1] else "0")
    return hex(int("".join(bits), 2))[2:].zfill(16)


def hamming(a: str, b: str) -> int:
    """两个 hex phash 的汉明距离（0=完全相同，64=完全不同）"""
    ia, ib = int(a, 16), int(b, 16)
    return bin(ia ^ ib).count("1")


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
    ap.add_argument("--prompt", required=True)
    ap.add_argument("--seed", type=int, default=-1)
    ap.add_argument("--resolution", default="512×512 (快速)")
    ap.add_argument("--quality", default="快速")
    ap.add_argument("--model", default="flux")
    ap.add_argument("--negative", default="")
    ap.add_argument("--config", default="", help="自定义 config 路径（进化线程用）")
    ap.add_argument("--no-cache", action="store_true", help="绕过图片缓存（进化 A/B 测试用，保持固定 seed 但强制真实生成）")
    args = ap.parse_args()

    # 关键：延迟 import，确保读到磁盘最新代码
    from backend.pipeline import Pipeline
    cfg_path = args.config or "D:/AI-Workshop/config.json"
    pipe = Pipeline(cfg_path)

    t0 = time.time()
    results = pipe.generate_images(
        prompt=args.prompt, count=1, model_key=args.model,
        quality_name=args.quality, resolution=args.resolution,
        seed=args.seed, negative=args.negative, skip_cache=args.no_cache,
    )
    elapsed = time.time() - t0

    out = {
        "success": False, "path": "", "phash": "", "elapsed": round(elapsed, 2),
        "seed": args.seed, "git": git_head(),
        "resolution": args.resolution, "quality": args.quality,
        "prompt": args.prompt[:80],
    }
    if results and results[0].success:
        p = results[0].output_path
        out["success"] = True
        out["path"] = p
        try:
            out["phash"] = dhash(p)
        except Exception as e:
            out["phash"] = f"ERR:{e}"
    else:
        err = results[0].error if results else "no result"
        out["error"] = str(err)[:200]

    # stdout 输出带标记前缀的 JSON（守护循环按标记提取，避免 logger 干扰）
    print("__GEN_RESULT__" + json.dumps(out, ensure_ascii=False))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
