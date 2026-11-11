"""验证显存看门狗（backend/idle_watchdog.py）真的会释放，而不是只起了个线程。

为什么值得单独测：看门狗是**后台线程 + 定时 + 一次性动作**，
最典型的失败模式是"线程起了、日志打了、但其实什么都没做" ——
光看启动日志完全看不出来。

测什么：
  ① 队列探测（有任务 = busy、没任务 = idle、服务没起 = None）三者能分清
  ② /free 真能打通
  ③ 整个 _loop：队列空 + 窗口设 0 → 真的调了 /free（而不是空转）
  ④ 队列非空时**不**释放（不能把人家正在跑的任务的模型卸掉）
  ⑤ start() 幂等：重复调用不会起第二个线程

跑法：python scripts/_idle-watchdog-verify.py
依赖：ComfyUI 在 8188 上（本机常态）。没有的话 ①②③ 会跳过并明确说明。
"""

import json
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend import idle_watchdog as iw  # noqa: E402

CFG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
PORTS = iw._ports(CFG)

passed = failed = 0


def check(label, ok, extra=""):
    global passed, failed
    passed, failed = (passed + 1, failed) if ok else (passed, failed + 1)
    print(f"  {'✅' if ok else '❌'} {label}  {extra}")


print("=" * 70)
print("显存看门狗验证")
print("=" * 70)
print(f"  端口: {PORTS}")

print("\n① 队列探测")
busy = iw._queue_busy(PORTS[0])
print(f"     _queue_busy({PORTS[0]}) = {busy}")
check("服务在跑时能拿到明确的是/否（不是 None）", busy is not None,
      "None 说明 ComfyUI 没起 —— 后面几项会失去意义")
check("返回的是布尔值（不是 None）", isinstance(busy, bool), type(busy).__name__)
check("★ 探测不存在的端口要返回 None（和「空队列」必须区分开）",
      iw._queue_busy(8199) is None, "8199 上没有任何服务")

print("\n② /free 能不能打通")
if busy is not None:
    ok_free = iw._free(PORTS[0])
    check("/free 返回 200", ok_free)
else:
    print("  ⚠️ ComfyUI 没起，跳过")

print("\n③ _loop：队列空 + 窗口 0 → 必须真的调一次 /free")
if busy is not None:
    calls = []
    real_free = iw._free

    def spy(port, timeout=10.0):
        calls.append(port)
        return True

    iw._free = spy
    iw.POLL_SECONDS = 0.2          # 别等 60 秒
    stop = threading.Event()
    t = threading.Thread(target=iw._loop, args=(PORTS, 0.0, stop), daemon=True)
    t.start()
    deadline = time.time() + 5
    while not calls and time.time() < deadline:
        time.sleep(0.1)
    stop.set()
    iw._free = real_free
    check("★ 队列空闲时真的调了 /free（不是空转）", len(calls) > 0, f"调了 {len(calls)} 次: {calls}")
    check("★ 只释放一次（不会每轮都卸模型）", len(calls) == len(PORTS),
          f"{len(calls)} 次 / {len(PORTS)} 个端口")
else:
    print("  ⚠️ 跳过")

print("\n④ 队列非空时不能释放（别把别人正在跑的任务卸了）")
calls2 = []
real_free2 = iw._free
real_busy = iw._queue_busy


def spy_busy(port, timeout=5.0):
    return True                    # 假装一直有任务


iw._free = lambda port, timeout=10.0: calls2.append(port) or True
iw._queue_busy = spy_busy
stop2 = threading.Event()
t2 = threading.Thread(target=iw._loop, args=(PORTS, 0.0, stop2), daemon=True)
t2.start()
time.sleep(0.8)
stop2.set()
iw._free = real_free2
iw._queue_busy = real_busy
check("★ 队列一直有任务 → 一次都不释放", len(calls2) == 0, f"调了 {len(calls2)} 次")

print("\n⑤ start() 幂等")
a = iw.start(CFG)
b = iw.start(CFG)
check("★ 重复调用只会有一个线程", a is b, f"{a.name if a else None}")

print()
print("=" * 70)
print(f"{'✅ 全部通过' if failed == 0 else '❌ 有失败'}（{passed} 项通过 / {failed} 项失败）")
print("=" * 70)
sys.exit(1 if failed else 0)
