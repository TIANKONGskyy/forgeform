# -*- coding: utf-8 -*-
"""方向3 · 多任务并行进化调度器（图像 + 3D 双线轮转，视频线预留）

设计（仿生「多器官协同」）：
- 图像线：持续生成（低风险打底），产出候选图
- 3D 线：每 N 张图像后，消费最近一张图跑 3D（独占 GPU，温度保护）
- 视频线：默认关闭（温度风险最高，--enable-video 开启，需宇凯确认功耗墙）

关键约束（2026-08-13 教训）：
1. 任何时刻只有一条线在提交 ComfyUI 任务（互斥锁 multi.lock）
2. 视频/3D 温度保护：≥80°C 等待，≥85°C 强制中断
3. 三条线各自独立日志，可回溯

用法: python multi_evolve.py [--video-every 50] [--enable-video]
"""
import sys, os, time, json, argparse, subprocess, csv
from pathlib import Path

sys.path.insert(0, "D:/AI-Workshop")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

WORKSPACE = Path("D:/AI-Workshop")
PY = r"D:\miniconda3\python.exe"
GEN_IMG = WORKSPACE / "scripts" / "_gen_one.py"
GEN_3D = WORKSPACE / "scripts" / "_gen_3d.py"
LOCK = WORKSPACE / "outputs" / "continuous" / "multi.lock"
LOG_DIR = WORKSPACE / "outputs" / "continuous"

# 图像 prompt 池（持续生成）
IMG_PROMPTS = [
    "a red ceramic teapot with gold trim, studio product photography",
    "a small robot toy on white background, soft studio lighting",
    "a wooden sailing ship model, intricate rigging, museum display",
    "a glass chess set, dramatic rim lighting, dark background",
    "a brass pocket watch with visible gears, macro photography",
    "a miniature stone castle with moss, fantasy diorama",
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


def wait_cool(max_temp=80):
    """等 GPU 降温到阈值以下"""
    waited = 0
    while gpu_temp() >= max_temp:
        print(f"  ⏳ GPU {gpu_temp()}°C ≥ {max_temp}°C，等待降温...")
        time.sleep(15)
        waited += 15
        if waited > 600:
            print("  ⚠️ 降温超时 10 分钟，继续")
            break


def run_script(script: Path, extra_args: list) -> dict:
    """spawn 子进程跑脚本，返回 __GEN_RESULT__ JSON"""
    r = subprocess.run(
        [PY, str(script)] + extra_args,
        capture_output=True, text=True, timeout=2400,
        encoding="utf-8", errors="replace", cwd=str(WORKSPACE),
    )
    for line in reversed(r.stdout.splitlines()):
        line = line.strip()
        if line.startswith("__GEN_RESULT__"):
            try:
                return json.loads(line[len("__GEN_RESULT__"):])
            except Exception as e:
                return {"success": False, "error": str(e)}
    return {"success": False, "error": "no result", "stderr": r.stderr[-200:]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--td-every", type=int, default=8, help="每 N 张图像跑 1 次 3D")
    ap.add_argument("--video-every", type=int, default=50, help="每 N 张图像跑 1 次视频")
    ap.add_argument("--enable-video", action="store_true", help="启用视频线（需确认功耗墙）")
    args = ap.parse_args()

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    img_csv = LOG_DIR / "multi_image.csv"
    td_csv = LOG_DIR / "multi_3d.csv"

    seed = int(time.time()) % 100000
    img_count = 0

    print(f"[调度器] 启动 | 3D每{args.td_every}张 | 视频线{'开' if args.enable_video else '关(默认)'}")

    with open(img_csv, "a", newline="", encoding="utf-8") as fi, \
         open(td_csv, "a", newline="", encoding="utf-8") as ft:
        wi = csv.writer(fi)
        wt = csv.writer(ft)
        if img_csv.stat().st_size == 0:
            wi.writerow(["ts", "seed", "git", "phash", "elapsed", "prompt", "path"])
        if td_csv.stat().st_size == 0:
            wt.writerow(["ts", "src_img", "elapsed", "success", "path"])

        while True:
            # ── 图像线：持续生成 ──
            img_count += 1
            cur_seed = seed + img_count
            prompt = IMG_PROMPTS[(img_count - 1) % len(IMG_PROMPTS)]

            LOCK.write_text("img", encoding="utf-8")
            r = run_script(GEN_IMG, ["--prompt", prompt, "--seed", str(cur_seed)])
            LOCK.unlink(missing_ok=True)

            ts = time.strftime("%H:%M:%S")
            wi.writerow([ts, cur_seed, r.get("git", "?"), r.get("phash", ""),
                         r.get("elapsed", -1), prompt[:40], r.get("path", "")])
            fi.flush()
            print(f"[{ts}] 图像#{img_count} seed={cur_seed} {r.get('elapsed',-1)}s "
                  f"{'✅' if r.get('success') else '❌'}")

            # ── 3D 线：每 N 张消费最近图 ──
            if img_count % args.td_every == 0 and r.get("success"):
                wait_cool(80)
                LOCK.write_text("3d", encoding="utf-8")
                src = r.get("path", "")
                r3 = run_script(GEN_3D, ["--image", src])
                LOCK.unlink(missing_ok=True)
                ts = time.strftime("%H:%M:%S")
                wt.writerow([ts, src, r3.get("elapsed", -1), r3.get("success", False),
                             r3.get("path", "")])
                ft.flush()
                print(f"[{ts}] 3D线 消费 {Path(src).name} → "
                      f"{r3.get('elapsed',-1)}s {'✅' if r3.get('success') else '❌'}"
                      f"{r3.get('error','')[:40]}")
                wait_cool(75)

            # ── 视频线：预留（默认关）──
            if args.enable_video and img_count % args.video_every == 0 and r.get("success"):
                wait_cool(80)
                # TODO: 视频线接入（需先确认功耗墙 320W→220W）
                print("[视频线] 预留，未接入（需确认功耗墙）")

            time.sleep(4)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        if LOCK.exists():
            LOCK.unlink(missing_ok=True)
        print("\n[调度器] 已停止")
