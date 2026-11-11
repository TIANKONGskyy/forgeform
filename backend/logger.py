"""Structured logger for AI Workshop — file + console output."""
import json
import time
import sys
from pathlib import Path
from backend.env import LOGS
from datetime import datetime


class Logger:
    def __init__(self, log_dir: str = str(LOGS)):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.log_file = self.log_dir / f"{datetime.now().strftime('%Y-%m-%d')}.jsonl"
        self._console = True

    def log(self, stage: str, action: str, status: str,
            duration_s: float = 0, error: str = "",
            params: dict = None, output: str = ""):
        entry = {
            "ts": datetime.now().isoformat(),
            "stage": stage,
            "action": action,
            "status": status,
        }
        if duration_s:
            entry["duration_s"] = round(duration_s, 1)
        if error:
            entry["error"] = error
        if params:
            entry["params"] = params
        if output:
            entry["output"] = output

        # Write to file
        with open(self.log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

        # Console summary
        if self._console:
            icon = "✅" if status == "completed" else ("❌" if status == "failed" else "⏳")
            dur = f" ({duration_s:.1f}s)" if duration_s else ""
            msg = f"  {icon} [{stage}] {action} → {status}{dur}"
            if error:
                msg += f" | {error[:80]}"
            print(msg, file=sys.stderr)

    def get_recent(self, n: int = 50) -> list[dict]:
        """Read last N log entries."""
        if not self.log_file.exists():
            return []
        lines = self.log_file.read_text(encoding="utf-8").strip().split("\n")
        entries = []
        for line in lines[-n:]:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                pass
        return entries


# Global singleton
_logger = None

def get_logger(name: str = None) -> Logger:
    """Get logger singleton. name param accepted for compatibility but ignored."""
    global _logger
    if _logger is None:
        _logger = Logger()
    return _logger


# Add convenience methods for compatibility
def _log_info(self, msg: str):
    self.log("info", msg, "info")

def _log_warning(self, msg: str):
    self.log("warning", msg, "warning")

def _log_error(self, msg: str):
    self.log("error", msg, "error")

def _log_debug(self, msg: str):
    self.log("debug", msg, "debug")

Logger.info = _log_info
Logger.warning = _log_warning
Logger.error = _log_error
Logger.debug = _log_debug
