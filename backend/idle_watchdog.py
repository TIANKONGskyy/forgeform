"""闲置时把 ComfyUI 占的显存还回去。

★ 为什么需要这个（2026-10-15，宇凯"一体化工作流"里的一项）
   实测：ComfyUI 跑完一次任务后会把模型**常驻显存** —— 单卡上看到 14.4GB 被占着。
   这本身是好事（连跑出图不用重载，热跑 3 秒 vs 冷跑 25 秒），
   但**闲置时它就是白占**：这条卡上还要跑对话模型（约 11GB）和视觉服务，
   谁要用谁就得等它让路，严重时直接装不下。

   来源是桌面《AI创意工坊_优化工程/02_界面与显存规划调研.md》的 P0 项 ——
   当时定了方案但没落地，现在补上。

   为什么不用 ComfyUI 自带机制：它**没有 idle 定时器**，只有"下一个任务来了
   再按需卸载"。那对"别的服务现在就要显存"这个场景完全没用 —— 它永远不会主动让。

★ 判定信号只用 ComfyUI 自己的队列（/queue 的 running + pending）
   出图 / 视频 / 3D 三条链都走同一个队列，所以"队列空了 N 分钟"等价于"没人用它"。
   好处：不必往 pipeline 里塞埋点，也就不会出现"新加了条链但忘了埋点"的漏洞。
   代价：刚提交、任务还没进队列的那几毫秒窗口可能被误判为闲置 ——
   但 /free 只是卸模型，下一个任务会重新加载，最坏是多一次冷加载，**不会出错**。

★ 为什么单独开线程
   这是**运维**职责（看门狗），不属于管线。塞进 pipeline 会让执行层去操心
   "显存该不该常驻"这种驻留策略 —— 那是 model_management 的活。
"""

import json
import threading
import time
import urllib.request
from pathlib import Path

#: 队列空多久之后释放（分钟）。可用 config.json 的 comfyui_idle.free_after_minutes 覆盖。
DEFAULT_IDLE_MINUTES = 10
#: 轮询间隔（秒）。60 秒足够 —— 释放早晚一分钟没有实际差别，没必要打得勤。
POLL_SECONDS = 60

_thread: threading.Thread | None = None
_lock = threading.Lock()


def _ports(config: dict) -> list[int]:
    """ComfyUI 端口列表（单卡时 image/media 同端口，去重）。"""
    cfg = config.get("comfyui_ports") or {}
    img = int(cfg.get("image", 8188))
    media = int(cfg.get("media", img))
    return sorted({img, media})


def _queue_busy(port: int, timeout: float = 5.0) -> bool | None:
    """队列里有正在跑或排队的任务吗。

    返回 None 表示**探测不到**（服务没起/刚重启）—— 和"空队列"必须区分开，
    否则服务重启期间会被当成闲置，白白触发一次无意义的 /free。
    """
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/queue", timeout=timeout) as r:
            d = json.loads(r.read().decode("utf-8"))
    except Exception:
        return None
    return bool(d.get("queue_running")) or bool(d.get("queue_pending"))


def _free(port: int, timeout: float = 10.0) -> bool:
    """POST /free 让 ComfyUI 卸模型 + 归还显存。"""
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/free",
            data=json.dumps({"unload_models": True, "free_memory": True}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def _loop(ports: list[int], idle_minutes: float, stop: threading.Event,
          also_unload_agent: bool = True) -> None:
    idle_since: float | None = None
    freed = False

    while not stop.is_set():
        busy = False
        reachable = False
        for p in ports:
            q = _queue_busy(p)
            if q is None:
                continue
            reachable = True
            busy = busy or q

        if not reachable:
            # 服务没起 / 刚重启：状态作废，重新计时
            idle_since, freed = None, False
        elif busy:
            idle_since, freed = None, False
        else:
            now = time.time()
            if idle_since is None:
                idle_since = now
            elif not freed and (now - idle_since) >= idle_minutes * 60:
                done = [p for p in ports if _free(p)]
                freed = True
                print(f"[idle-watchdog] ComfyUI 已闲置 {idle_minutes:.0f} 分钟 → "
                      f"释放显存（端口 {done or '全部失败'}）")

                """
                ★ 2026-10-16 扩展：连**微调对话模型**一起卸。

                原来只 free ComfyUI —— 但本机最大的常驻占用其实是对话模型
                （实测 10.4GB，比 ComfyUI 跑完留下的 14GB 更"死"：
                 它 warm 着是为了首句快，但长时间没人说话时纯属白占）。

                宇凯的要求是"跑完之后及时卸载显存内存，以便跑后续项目"，
                所以闲置判定成立时，两个大占用者都该让路。

                ⚠️ 为什么要判"对话模型是否在使用"：
                   /unload 会把模型卸掉，下一次说话要重新冷加载（约 77s）。
                   ComfyUI 的队列能反映"有没有在用"，但对话没有队列 ——
                   **没法从这个线程里知道用户是不是正要说话**。
                   所以这里只在**ComfyUI 也闲置**（说明整个工作流没人动）
                  时才卸它，把"正在对话"的误伤概率压到最低。
                   宁可少卸一次，也别让用户说完话等 77 秒。
                """
                if also_unload_agent and _agent_idle():
                    try:
                        req = urllib.request.Request(
                            "http://127.0.0.1:8082/unload", data=b"{}",
                            headers={"Content-Type": "application/json"}, method="POST",
                        )
                        with urllib.request.urlopen(req, timeout=15) as r:
                            if r.status == 200:
                                print("[idle-watchdog] 对话模型也一并卸载（释放约 10GB）")
                    except Exception as e:
                        print(f"[idle-watchdog] 对话模型卸载失败（忽略）: {e!r}")

        stop.wait(POLL_SECONDS)


def _agent_idle(timeout: float = 5.0) -> bool:
    """对话模型当前是否"装着但没在用"。

    ★ 判据只有 loaded==True 这一条 —— 我们**看不到**用户的输入事件，
      所以这里不试图猜"他是不是在打字"。真正防误伤靠的是调用时机：
      只在 ComfyUI 也闲置时才走到这里（见 _loop 里的注释）。

    返回 False 表示"探不到/已经卸载"，两种情况都不需要动作。
    """
    try:
        with urllib.request.urlopen("http://127.0.0.1:8082/health", timeout=timeout) as r:
            d = json.loads(r.read().decode("utf-8"))
        return d.get("loaded") is True
    except Exception:
        return False


def start(config: dict) -> threading.Thread | None:
    """启动看门狗线程（幂等：重复调用只会有一个线程）。

    config 用 Pipeline 那份（读 comfyui_ports 和可选的 comfyui_idle.free_after_minutes）。
    """
    global _thread
    with _lock:
        if _thread is not None and _thread.is_alive():
            return _thread

        ports = _ports(config)
        idle_minutes = float(
            (config.get("comfyui_idle") or {}).get("free_after_minutes", DEFAULT_IDLE_MINUTES)
        )
        # 是否连对话模型一起卸（可用 config.json 的 comfyui_idle.unload_agent=false 关掉）
        also_agent = bool((config.get("comfyui_idle") or {}).get("unload_agent", True))
        stop = threading.Event()
        _thread = threading.Thread(
            target=_loop, args=(ports, idle_minutes, stop, also_agent),
            name="comfyui-idle-watchdog", daemon=True,
        )
        _thread.start()
        print(f"[idle-watchdog] 已启动：端口 {ports}，闲置 {idle_minutes:.0f} 分钟释放显存"
              f"{'（含对话模型）' if also_agent else ''}")
        return _thread
