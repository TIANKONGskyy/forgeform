# -*- coding: utf-8 -*-
"""GPU 温度调节器：渐进降频 + 迟滞回温（宇凯 2026-08-18 指定策略）
- 温度 > 81°C：开始逐步降频（按温度阶梯锁 max graphics 时钟，越高越降）
- 温度 ≤ 77°C：恢复正常频率（解锁）
- 77~81°C 之间：保持当前状态（迟滞，防来回抖动）
- 不硬掐任务；温度 ≥90°C 冗余线也只用到最强降频（45%），不暂停
注意：nvidia-smi -lgc/-rgc 需要管理员权限，请用 setup_thermal_gov.ps1 以管理员注册开机自启。
"""
import subprocess, time, os, sys
from pathlib import Path

_LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
_LOG_DIR.mkdir(parents=True, exist_ok=True)

POLL = 5             # 轮询间隔（秒）
THROTTLE_START = 81  # 开始降频阈值 °C
RESTORE_AT = 77      # 恢复正常频率阈值 °C
EMERGENCY = 90       # 冗余线 °C：超过则用最强降频兜底
MIN_CLK = 210        # 最低允许时钟（MHz，base clock）
FLOOR_RATIO = 0.45   # 最强降频比例（留有冗余，不归零）

# 温度阶梯 → 最大时钟比例（渐进式）
CURVE = [
    (81, 0.95), (82, 0.90), (83, 0.82), (84, 0.74),
    (85, 0.66), (86, 0.58), (87, 0.52), (88, 0.45),
]

LOG = str(_LOG_DIR / "thermal_governor.log")
HEARTBEAT = str(_LOG_DIR / "thermal_governor.heartbeat")


def run(args, timeout=8):
    try:
        subprocess.run(["nvidia-smi"] + args, capture_output=True, timeout=timeout,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return True
    except Exception:
        return False


def get_temp():
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=temperature.gpu", "--format=csv,noheader"],
            timeout=8, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).decode().strip()
        return int(out.split()[0])
    except Exception:
        return None


def get_max_clk():
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=clocks.max.graphics", "--format=csv,noheader"],
            timeout=8, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).decode().strip()
        return int(out.split()[0])
    except Exception:
        return None


def ratio_for(temp):
    """按温度取降频比例（渐进）"""
    if temp >= EMERGENCY:
        return FLOOR_RATIO
    if temp > THROTTLE_START:
        r = FLOOR_RATIO
        for t0, rr in CURVE:
            if temp >= t0:
                r = rr
            else:
                break
        return r
    return None


def main():
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    max_clk = get_max_clk() or 2100
    state = "normal"       # normal | throttled
    applied_ratio = None
    warned_perm = False
    msg = f"[thermal_governor] 启动: max_graphics={max_clk}MHz, 降频>{THROTTLE_START}°C, 恢复<={RESTORE_AT}°C"
    print(msg, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")

    while True:
        t = get_temp()
        if t is not None:
            ratio = ratio_for(t)
            if ratio is not None:
                # 需要降频（含阶梯变化时重新应用更松/更紧的锁）
                if applied_ratio != ratio:
                    lock_max = int(max_clk * ratio)
                    ok = run(["-lgc", f"{MIN_CLK},{lock_max}"])
                    if ok:
                        msg = f"{t}°C → 降频至 {lock_max}MHz ({int(ratio * 100)}%)"
                        print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)
                        with open(LOG, "a", encoding="utf-8") as f:
                            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
                        state, applied_ratio = "throttled", ratio
                    elif not warned_perm:
                        warned_perm = True
                        msg = ("[警告] nvidia-smi 改时钟失败（需要管理员权限）——"
                               "请用管理员运行 setup_thermal_gov.ps1 注册开机自启，或管理员方式运行本脚本")
                        print(msg, flush=True)
                        with open(LOG, "a", encoding="utf-8") as f:
                            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
            else:
                # 温度回落到恢复线以下 → 恢复正常频率
                if state == "throttled" and t <= RESTORE_AT:
                    run(["-rgc"])
                    msg = f"{t}°C → 恢复正常频率"
                    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)
                    with open(LOG, "a", encoding="utf-8") as f:
                        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
                    state, applied_ratio = "normal", None
        time.sleep(POLL)
        # 心跳：APP 用它判断调节器是否在运行（免子进程查询）
        try:
            with open(HEARTBEAT, "w") as f:
                f.write(str(time.time()))
        except Exception:
            pass


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        run(["-rgc"])
        print("已退出并恢复频率")
