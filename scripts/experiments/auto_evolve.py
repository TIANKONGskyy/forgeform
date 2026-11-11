# -*- coding: utf-8 -*-
"""方向1 · 自动进化闭环（仿生：改参数 → A/B 测试 → 保留更优 → 记录谱系）

在【不停止守护循环】的前提下，独立进化线程对可调参数做 A/B 测试：
1. 候选参数空间：TeaCache 阈值 (0.2/0.4/0.6/0.8) × 步数乘子
2. 每个候选：固定 seed 生成 benchmark 图 → 记录 phash + 耗时
3. 与当前基线对比：
   - phash 汉明距离小（视觉差异小）= 质量保持
   - 耗时短 = 效率提升
4. 综合评分选出「质量损失最小 + 加速最大」的参数，写回 config 作为进化结果
5. 全程记录到 evolution_log.jsonl（谱系，可回溯）

安全设计：
- 只动 config 里的 teacache_thresh / steps，不改核心工作流结构
- 任何候选都可用 git + config 回退
- 温度自适应：>80°C 暂停进化，等守护循环降温

用法: python auto_evolve.py [--rounds 6] [--save-config]
"""
import sys, os, json, time, argparse, subprocess
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

WORKSPACE = str(Path(__file__).resolve().parent.parent.parent)
GEN_SCRIPT = Path(WORKSPACE) / "scripts" / "experiments" / "_gen_one.py"
PY = sys.executable
EVO_LOG = Path(WORKSPACE) / "outputs" / "continuous" / "evolution_log.jsonl"
EVO_LOCK = Path(WORKSPACE) / "outputs" / "continuous" / "evolve.lock"

# 进化用的固定评估集（与守护循环的 prompt 池不同 seed，避免污染对比）
EVAL_SET = [
    ("a brass pocket watch with visible gears, macro photography", 70001),
    ("a miniature stone castle with moss, fantasy diorama", 70002),
]

# 候选参数空间（TeaCache 阈值：越大越快但质量损失越大）
TEA_THRESH_CANDIDATES = [0.2, 0.4, 0.6, 0.8]


def hamming(a: str, b: str) -> int:
    if not a or not b or a.startswith("ERR") or b.startswith("ERR"):
        return 64
    try:
        return bin(int(a, 16) ^ int(b, 16)).count("1")
    except Exception:
        return 64


def gpu_temp() -> int:
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=temperature.gpu",
             "--format=csv,noheader,nounits"],
            stderr=subprocess.DEVNULL, timeout=5).decode().strip()
        return int(out.split("\n")[0].strip())
    except Exception:
        return 60


def gen_with_thresh(prompt: str, seed: int, thresh: float) -> dict:
    """用指定 TeaCache 阈值生成一张图（通过临时 config 覆盖）"""
    # 写临时 config 覆盖（子进程读临时 config）
    tmp_cfg = Path(WORKSPACE) / "config.evolve.json"
    import shutil
    base = Path(WORKSPACE) / "config.json"
    cfg = json.loads(base.read_text(encoding="utf-8"))
    cfg["models"]["image"]["flux"]["teacache_thresh"] = thresh
    cfg["models"]["image"]["flux"]["teacache"] = thresh > 0
    tmp_cfg.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    # 队列感知：提交前等 ComfyUI 队列清空（避免并发压垮，2026-08-13 教训）
    import requests
    for _ in range(120):
        try:
            q = requests.get("http://127.0.0.1:8188/queue", timeout=5).json()
            if len(q.get("queue_running", [])) == 0 and len(q.get("queue_pending", [])) == 0:
                break
        except Exception:
            pass
        time.sleep(3)

    # 直接用子进程 + 临时 config（需 _gen_one 支持 --config）
    r = subprocess.run(
        [PY, str(GEN_SCRIPT), "--prompt", prompt, "--seed", str(seed),
         "--config", str(tmp_cfg), "--no-cache"],
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=6)
    ap.add_argument("--save-config", action="store_true", help="把最优参数写回 config.json")
    args = ap.parse_args()

    log_dir = EVO_LOG.parent
    log_dir.mkdir(parents=True, exist_ok=True)

    # 互斥锁：进化期间守护循环暂停（避免并发压魔 ComfyUI）
    EVO_LOCK.write_text("evolving", encoding="utf-8")
    print(f"[进化] 已加锁 {EVO_LOCK.name}，守护循环暂停提交")

    print(f"=== 方向1 · 自动进化闭环（{args.rounds} 轮）===")
    print(f"评估集: {len(EVAL_SET)} 项 | 候选 TeaCache 阈值: {TEA_THRESH_CANDIDATES}")

    # 基线：thresh=0（当前默认 0.4 作为对比基线先跑一次）
    print("\n[基线] thresh=0.4（当前生产值）")
    baseline = {}
    for prompt, seed in EVAL_SET:
        r = gen_with_thresh(prompt, seed, 0.4)
        baseline[seed] = r
        print(f"  seed={seed} {r.get('elapsed',-1)}s phash={r.get('phash','')[:16]} "
              f"{'✅' if r.get('success') else '❌'}")

    for round_i in range(1, args.rounds + 1):
        print(f"\n--- 第 {round_i}/{args.rounds} 轮 ---")
        for thresh in TEA_THRESH_CANDIDATES:
            # 温度保护：太高就等
            while gpu_temp() >= 80:
                print(f"  ⏳ GPU {gpu_temp()}°C 偏高，暂停进化等待降温...")
                time.sleep(15)

            results = {}
            total_dist = 0
            total_time = 0.0
            ok = 0
            for prompt, seed in EVAL_SET:
                r = gen_with_thresh(prompt, seed, thresh)
                results[seed] = r
                if r.get("success"):
                    ok += 1
                    total_time += r.get("elapsed", 0)
                    total_dist += hamming(baseline[seed].get("phash", ""), r.get("phash", ""))
            if ok == 0:
                print(f"  thresh={thresh}: ❌ 全部失败")
                continue

            avg_time = round(total_time / ok, 2)
            avg_dist = round(total_dist / ok, 1)
            seeds = [s for _, s in EVAL_SET]
            base_time = sum(baseline[s].get("elapsed", 0) for s in seeds) / len(seeds)
            speedup = round(base_time / avg_time, 2) if avg_time > 0 else 0

            # 评分：加速多 + 距离小 = 好（距离权重大，质量优先）
            # score = speedup - avg_dist * 0.05（每 1 距离惩罚 0.05 加速倍）
            score = round(speedup - avg_dist * 0.05, 3)

            record = {
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                "round": round_i,
                "thresh": thresh,
                "avg_time": avg_time,
                "avg_dist": avg_dist,
                "speedup": speedup,
                "score": score,
                "gpu_temp": gpu_temp(),
            }
            with open(EVO_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")

            print(f"  thresh={thresh}: {avg_time}s (加速 {speedup}x) "
                  f"phash距离 {avg_dist} 得分 {score}")

    print(f"\n✅ 进化日志: {EVO_LOG}")

    # 汇总：找出历史最高得分参数
    if EVO_LOG.exists():
        best = None
        for line in EVO_LOG.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            if best is None or r["score"] > best["score"]:
                best = r
        if best:
            print(f"\n🏆 历史最优: thresh={best['thresh']} 加速 {best['speedup']}x "
                  f"距离 {best['avg_dist']} 得分 {best['score']} (第 {best['round']} 轮)")
            if args.save_config and best["thresh"] != 0.4:
                cfg_path = Path(WORKSPACE) / "config.json"
                cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
                cfg["models"]["image"]["flux"]["teacache_thresh"] = best["thresh"]
                cfg["models"]["image"]["flux"]["teacache"] = best["thresh"] > 0
                cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
                print(f"✅ 已把最优 thresh={best['thresh']} 写回 config.json")

    # 释放互斥锁，让守护循环恢复
    if EVO_LOCK.exists():
        EVO_LOCK.unlink()
        print("[进化] 已释放锁，守护循环恢复")


if __name__ == "__main__":
    try:
        main()
    finally:
        # 异常退出也确保释放锁
        if EVO_LOCK.exists():
            EVO_LOCK.unlink()
            print("[进化] 锁已释放（finally）")
