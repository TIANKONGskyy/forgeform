# -*- coding: utf-8 -*-
"""持续生成守护循环（仿生热更新 + 70% 性能占空比）

核心设计：
1. 主循环【不 import backend】，每次生成 spawn 子进程 _gen_one.py
   → 子进程读磁盘最新 backend 代码 → 我改代码，下一次生成自动用新版，主循环永不中断
2. 占空比控制：生成一张（~10s）+ 间隔 sleep，让 GPU 平均利用率 ~70%
3. 温度自适应：GPU 温度升高时自动加大间隔，降温后恢复
4. 每次生成记录 (seed, git, phash, elapsed, path) → generation_log.csv
   → 支持"改前 vs 改后"固定 seed 反复比对

用法: python continuous_generate.py [--interval 4] [--target-util 70]
"""
import subprocess, sys, os, time, json, csv, argparse
from pathlib import Path

sys.path.insert(0, "D:/AI-Workshop")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

WORKSPACE = "D:/AI-Workshop"
LOG_CSV = Path(WORKSPACE) / "outputs" / "continuous" / "generation_log.csv"
GEN_SCRIPT = Path(WORKSPACE) / "scripts" / "_gen_one.py"
PY = r"D:\miniconda3\python.exe"
EVO_LOCK = Path(WORKSPACE) / "outputs" / "continuous" / "evolve.lock"

# prompt 池：多样化题材，持续生成覆盖多类（可随时编辑此文件热更新）
PROMPTS = [
    "a red ceramic teapot with gold trim, studio product photography",
    "a small robot toy on white background, soft studio lighting",
    "a wooden sailing ship model, intricate rigging, museum display",
    "a glass chess set, dramatic rim lighting, dark background",
    "a brass pocket watch with visible gears, macro photography",
    "a miniature stone castle with moss, fantasy diorama",
    "a chrome sphere on reflective surface, abstract minimalism",
    "a knitted wool scarf, cozy warm tones, flat lay",
]


def gpu_temp() -> int:
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=temperature.gpu",
             "--format=csv,noheader,nounits"],
            stderr=subprocess.DEVNULL, timeout=5,
        ).decode().strip()
        return int(out.split("\n")[0].strip())
    except Exception:
        return 60


def gpu_util() -> int:
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=utilization.gpu",
             "--format=csv,noheader,nounits"],
            stderr=subprocess.DEVNULL, timeout=5,
        ).decode().strip()
        return int(out.split("\n")[0].strip())
    except Exception:
        return 0


def gen_one(prompt: str, seed: int) -> dict:
    """spawn 子进程生成一张图，返回解析后的 JSON"""
    try:
        r = subprocess.run(
            [PY, str(GEN_SCRIPT), "--prompt", prompt, "--seed", str(seed)],
            capture_output=True, text=True, timeout=300,
            encoding="utf-8", errors="replace",
            cwd=WORKSPACE,
        )
        # 从 stdout 提取 __GEN_RESULT__ 标记后的 JSON（避免 logger 干扰）
        for line in reversed(r.stdout.splitlines()):
            line = line.strip()
            if line.startswith("__GEN_RESULT__"):
                try:
                    return json.loads(line[len("__GEN_RESULT__"):])
                except Exception as e:
                    return {"success": False, "error": f"JSON parse: {e}"}
    except Exception as e:
        return {"success": False, "error": str(e)}
    return {"success": False, "error": "no output"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=float, default=4.0, help="每张图之间的间隔秒数")
    ap.add_argument("--max-iter", type=int, default=0, help="0=无限循环")
    args = ap.parse_args()

    log_dir = LOG_CSV.parent
    log_dir.mkdir(parents=True, exist_ok=True)
    new_file = not LOG_CSV.exists()

    # 递增 seed：同一 prompt 每次 seed 不同，可对比时间/质量
    seed = int(time.time()) % 100000

    print(f"[守护] 持续生成启动 | interval={args.interval}s | prompt 池 {len(PROMPTS)} 条")
    print(f"[守护] 日志: {LOG_CSV}")

    i = 0
    with open(LOG_CSV, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["ts", "iter", "seed", "git", "phash", "elapsed_s",
                        "success", "prompt", "path", "gpu_temp"])

        while True:
            i += 1
            if args.max_iter and i > args.max_iter:
                break

            prompt = PROMPTS[(i - 1) % len(PROMPTS)]
            cur_seed = seed + i

            # 互斥锁：进化闭环运行时，守护循环暂停提交（避免并发压魔 ComfyUI，2026-08-13 教训）
            if EVO_LOCK.exists():
                time.sleep(5)
                continue

            # 温度自适应：>80°C 大幅加间隔，>75°C 适度加
            t = gpu_temp()
            extra = 0.0
            if t >= 82:
                extra = 15.0
            elif t >= 78:
                extra = 8.0
            elif t >= 74:
                extra = 3.0

            res = gen_one(prompt, cur_seed)
            ts = time.strftime("%H:%M:%S")
            w.writerow([ts, i, cur_seed, res.get("git", "?"), res.get("phash", ""),
                        res.get("elapsed", -1), res.get("success", False),
                        prompt[:60], res.get("path", ""), t])
            f.flush()

            status = "✅" if res.get("success") else f"❌{res.get('error','')[:40]}"
            print(f"[{ts}] #{i} seed={cur_seed} git={res.get('git','?')} "
                  f"{res.get('elapsed',-1)}s {status} GPU={t}°C")

            # 占空比：生成 + 间隔，让平均利用率 ~70%
            sleep_s = args.interval + extra
            time.sleep(sleep_s)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[守护] 已停止")
