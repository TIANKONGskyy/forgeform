"""Hardware monitoring — detailed metrics with power estimation."""
import subprocess
import time
import psutil


class HardwareMonitor:
    """Real-time hardware monitoring with history tracking."""

    def __init__(self, history_size: int = 60):
        self.history_size = history_size
        self.history = {
            "cpu": [], "gpu": [], "ram": [], "vram": [],
            "gpu_temp": [], "gpu_power": [],
        }
        self._last_update = 0
        self._cache = None
        self._cache_time = 0
        self._percore_time = 0
        self._gpu_power_limit = 0  # watts
        self._cpu_model = None
        self._gpu_model = None
        self._cpu_power_limit = 240  # EPYC 7R13 TDP
        # 平滑状态（EMA，2026-08-11 新增：数值不跳变）
        self._ema = {"cpu": None, "gpu": None, "ram": None, "vram": None,
                     "gpu_temp": None, "gpu_power": None}
        self._ema_alpha = 0.35  # 越大响应越快，越小越平滑

    def _smooth(self, key: str, value: float) -> float:
        """EMA 平滑：突发尖峰被削平，整体平顺"""
        prev = self._ema.get(key)
        if prev is None:
            self._ema[key] = float(value)
        else:
            self._ema[key] = self._ema_alpha * float(value) + (1 - self._ema_alpha) * prev
        return round(self._ema[key])

    def _update_history(self, key: str, value: float):
        h = self.history[key]
        h.append(value)
        if len(h) > self.history_size:
            h.pop(0)

    def get_cpu_model(self) -> str:
        """Get CPU model name (cached)."""
        if self._cpu_model:
            return self._cpu_model
        try:
            out = subprocess.check_output(
                ['wmic', 'cpu', 'get', 'name', '/value'],
                stderr=subprocess.DEVNULL, timeout=5
            ).decode('utf-8', errors='ignore')
            for line in out.split('\n'):
                if line.strip().startswith('Name='):
                    self._cpu_model = line.split('=', 1)[1].strip()
                    return self._cpu_model
        except Exception:
            pass
        self._cpu_model = "Unknown CPU"
        return self._cpu_model

    def get_gpu_model(self) -> str:
        """Get GPU model name (cached). 2026-08-20: dual-card aware."""
        if self._gpu_model:
            return self._gpu_model
        try:
            out = subprocess.check_output(
                ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader,nounits"],
                stderr=subprocess.DEVNULL, timeout=5,
            ).decode().strip()
            lines = [l.strip() for l in out.splitlines() if l.strip()]
            if len(lines) > 1:
                self._gpu_model = f"{lines[0]} ×{len(lines)}"
            else:
                self._gpu_model = lines[0] if lines else "Unknown GPU"
            return self._gpu_model
        except Exception:
            pass
        self._gpu_model = "Unknown GPU"
        return self._gpu_model

    def get_gpu_power_limit(self) -> int:
        """Get current GPU power limit in watts."""
        if self._gpu_power_limit > 0:
            return self._gpu_power_limit
        try:
            out = subprocess.check_output(
                ["nvidia-smi", "--query-gpu=power.limit",
                 "--format=csv,noheader,nounits"],
                stderr=subprocess.DEVNULL, timeout=5,
            ).decode().strip()
            self._gpu_power_limit = int(float(out.split("\n")[0]))
        except Exception:
            self._gpu_power_limit = 320  # default
        return self._gpu_power_limit

    def get_stats(self) -> dict:
        """Return comprehensive hardware stats (cached 1s)."""
        now = time.time()
        # Real throttle: serve cached snapshot for 1s
        if self._cache is not None and now - self._cache_time < 1.0:
            return self._cache
        self._cache_time = now

        # CPU
        cpu_pct = psutil.cpu_percent(interval=0.1)
        cpu_count = psutil.cpu_count()
        cpu_freq = psutil.cpu_freq()
        # Per-core is expensive on 96 threads: refresh at most every 3s
        if now - self._percore_time >= 3.0:
            self._percore_time = now
            cpu_per_core = psutil.cpu_percent(interval=0.05, percpu=True)
        else:
            cpu_per_core = getattr(self, '_last_per_core', [])
        self._last_per_core = cpu_per_core

        # RAM
        ram = psutil.virtual_memory()
        ram_used_gb = round(ram.used / (1024**3), 1)
        ram_total_gb = round(ram.total / (1024**3), 1)

        # GPU (NVIDIA) - extended query (2026-08-20: dual-card aware, aggregates both GPUs)
        gpu_pct = gpu_mem_used = gpu_mem_total = gpu_temp = 0
        gpu_power = gpu_power_limit = 0
        gpu_clock = gpu_mem_clock = 0
        gpu_cards = []
        try:
            out = subprocess.check_output(
                ["nvidia-smi",
                 "--query-gpu=utilization.gpu,memory.used,memory.total,"
                 "temperature.gpu,power.draw,power.limit,"
                 "clocks.current.graphics,clocks.current.memory",
                 "--format=csv,noheader,nounits"],
                stderr=subprocess.DEVNULL, timeout=5,
            ).decode().strip()
            utils, useds, totals, temps, powers, limits = [], [], [], [], [], []
            for line in out.splitlines():
                if not line.strip():
                    continue
                p = [x.strip() for x in line.split(",")]
                utils.append(int(p[0])); useds.append(int(p[1])); totals.append(int(p[2]))
                temps.append(int(p[3])); powers.append(float(p[4])); limits.append(float(p[5]))
                gpu_cards.append({
                    "util": int(p[0]),
                    "mem_used_gb": round(int(p[1]) / 1024, 1),
                    "mem_total_gb": round(int(p[2]) / 1024, 1),
                    "temp": int(p[3]),
                    "power_w": round(float(p[4]), 0),
                    "power_limit_w": round(float(p[5]), 0),
                    "clock_mhz": int(p[6]) if len(p) > 6 else 0,
                    "mem_clock_mhz": int(p[7]) if len(p) > 7 else 0,
                })
            if utils:
                gpu_pct = round(sum(utils) / len(utils))
                gpu_mem_used = round(sum(useds) / 1024, 1)
                gpu_mem_total = round(sum(totals) / 1024, 1)
                gpu_temp = max(temps)
                gpu_power = round(sum(powers), 0)
                gpu_power_limit = round(sum(limits), 0)
                gpu_clock = max(c["clock_mhz"] for c in gpu_cards)
                gpu_mem_clock = max(c.get("mem_clock_mhz", 0) for c in gpu_cards)
        except Exception:
            pass

        gpu_mem_pct = round(gpu_mem_used / gpu_mem_total * 100) if gpu_mem_total else 0

        # CPU power estimation
        cpu_power_est = round(self._cpu_power_limit * cpu_pct / 100, 0)

        # EMA 平滑（2026-08-11）：显示值平顺不跳变
        cpu_disp = self._smooth("cpu", cpu_pct)
        gpu_disp = self._smooth("gpu", gpu_pct)
        ram_disp = self._smooth("ram", ram.percent)
        vram_disp = self._smooth("vram", gpu_mem_pct)
        gpu_temp_disp = self._smooth("gpu_temp", gpu_temp)
        gpu_power_disp = round(self._smooth("gpu_power", gpu_power))

        # Update history
        self._update_history("cpu", cpu_disp)
        self._update_history("gpu", gpu_disp)
        self._update_history("ram", ram_disp)
        self._update_history("vram", vram_disp)
        self._update_history("gpu_temp", gpu_temp_disp)
        self._update_history("gpu_power", gpu_power_disp)

        self._cache = {
            # Models
            "cpu_model": self.get_cpu_model(),
            "gpu_model": self.get_gpu_model(),
            # CPU
            "cpu": cpu_disp,
            "cpu_cores": cpu_count,
            "cpu_per_core": cpu_per_core,
            "cpu_freq_mhz": round(cpu_freq.current) if cpu_freq else 0,
            "cpu_power_w": cpu_power_est,
            "cpu_power_limit_w": self._cpu_power_limit,
            # GPU
            "gpu": gpu_disp,
            "gpu_temp": gpu_temp_disp,
            "gpu_power_w": gpu_power_disp,
            "gpu_power_limit_w": gpu_power_limit,
            "gpu_power_pct": round(gpu_power_disp / gpu_power_limit * 100) if gpu_power_limit else 0,
            "gpu_clock_mhz": gpu_clock,
            "gpu_mem_clock_mhz": gpu_mem_clock,
            "gpu_cards": gpu_cards,  # per-card detail (dual-GPU), frontend optional
            # RAM
            "ram": ram_disp,
            "ram_used_gb": ram_used_gb,
            "ram_total_gb": ram_total_gb,
            # VRAM
            "vram": vram_disp,
            "vram_used_gb": gpu_mem_used,
            "vram_total_gb": gpu_mem_total,
            # History (for sparklines)
            "cpu_hist": self.history["cpu"][-30:],
            "gpu_hist": self.history["gpu"][-30:],
            "ram_hist": self.history["ram"][-30:],
            "vram_hist": self.history["vram"][-30:],
            "gpu_temp_hist": self.history["gpu_temp"][-30:],
            "gpu_power_hist": self.history["gpu_power"][-30:],
        }
        return self._cache


# Global singleton
_monitor = HardwareMonitor()


def get_stats() -> dict:
    return _monitor.get_stats()


def get_monitor() -> HardwareMonitor:
    return _monitor
