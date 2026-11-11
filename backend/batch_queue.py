# -*- coding: utf-8 -*-
"""批量队列：多个提示词自动顺序执行 图像→3D→STL"""
import threading
import time
import traceback
from pathlib import Path

STATUS_ICONS = {
    "pending": "⏳",
    "running": "🔄",
    "done": "✅",
    "failed": "❌",
}


class BatchQueue:
    MAX_TASKS = 50  # 2026-08-13：队列上限（与 UI 提示一致）
    def __init__(self, pipe):
        self.pipe = pipe
        self.tasks = []          # [{prompt, status, img, glb, stl, error}]
        self.running = False
        self._thread = None
        self.current_idx = -1
        self._lock = threading.Lock()

    # ─── 任务管理 ───────────────────────────────
    def set_prompts(self, prompts):
        """设置任务列表（清空旧的，2026-08-13：强制截断到 MAX_TASKS=50）"""
        with self._lock:
            self.tasks = [{"prompt": p.strip(), "status": "pending",
                           "img": "", "glb": "", "stl": "", "error": "", "stage": ""}
                          for p in prompts if p.strip()][:self.MAX_TASKS]
            self.current_idx = -1
        return len(self.tasks)

    def append_prompt(self, prompt):
        with self._lock:
            if len(self.tasks) >= self.MAX_TASKS:
                return len(self.tasks)
            self.tasks.append({"prompt": prompt.strip(), "status": "pending",
                               "img": "", "glb": "", "stl": "", "error": "", "stage": ""})
        return len(self.tasks)

    def clear(self):
        with self._lock:
            self.tasks = []
            self.current_idx = -1

    # ─── 运行控制 ───────────────────────────────
    def start(self):
        if self.running:
            return False
        if not self.tasks:
            return False
        self.running = True
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()
        return True

    def stop(self):
        self.running = False

    def is_running(self):
        return self.running

    # ─── 工作线程 ───────────────────────────────
    def _worker(self):
        tasks = list(self.tasks)
        for i, task in enumerate(tasks):
            if not self.running:
                break
            # 温度自适应冷却（2026-08-14 新增）：
            # 批量连续跑重负载（图→3D→STL），任务间隔温度若不回落，
            # 下一个任务会立刻升温冲过 83°C 红线 → 等待冷却再开工
            if not self._cool_if_hot(task):
                if not self.running:
                    break          # 用户停止
                continue           # 持续高温超时：该任务已标记 failed，继续下一个
            with self._lock:
                self.current_idx = i
                task["status"] = "running"
                task["error"] = ""
                task["stage"] = ""
            # 失败自动重试（最多 3 次，2026-08-12 增强）
            attempts = 0
            while True:
                attempts += 1
                try:
                    self._run_one(task, i)
                    task["status"] = "done"
                    task["stage"] = ""
                    break
                except Exception as e:
                    task["error"] = str(e)[:200]
                    traceback.print_exc()
                    if attempts >= 3 or not self.running:
                        task["status"] = "failed"
                        task["stage"] = ""
                        break
                    with self._lock:
                        task["stage"] = f"⚠️ 失败重试 {attempts}/2"
                    time.sleep(3)
        self.running = False
        self.current_idx = -1

    # ─── 温度自适应冷却（2026-08-14）──────────────────
    COOL_START = 82   # ≥82°C：开始冷却等待（接近 83°C 崩溃红线）
    COOL_RESUME = 76  # 回落到 ≤76°C 恢复任务
    COOL_MAX_WAIT = 600  # 冷却等待上限 10 分钟，超时标记失败

    def _cool_if_hot(self, task) -> bool:
        """任务启动前检查 GPU 温度，过高则等待冷却。

        返回 True = 可以开始任务；False = 用户停止或持续高温超时（任务失败）。
        """
        get_temp = getattr(self.pipe.comfy, "get_gpu_temp", None)
        if get_temp is None:
            return True
        try:
            t = get_temp()
        except Exception:
            return True  # 温度读不到时不阻塞（单任务看门狗兜底）
        if t is None or t < self.COOL_START:
            return True
        waited = 0
        while self.running and waited < self.COOL_MAX_WAIT:
            with self._lock:
                task["status"] = "pending"
                task["stage"] = f"🌡️ 温度过高 {t}°C，等待冷却到 {self.COOL_RESUME}°C... ({waited}s)"
            time.sleep(15)
            waited += 15
            try:
                t = get_temp()
            except Exception:
                t = None
            if t is not None and t <= self.COOL_RESUME:
                with self._lock:
                    task["stage"] = ""
                return True
        if not self.running:
            return False
        with self._lock:
            task["status"] = "failed"
            task["error"] = f"GPU 持续高温（≥{self.COOL_START}°C 超过 {self.COOL_MAX_WAIT//60} 分钟），已跳过"
            task["stage"] = ""
        return False

    def _run_one(self, task, idx: int):
        pipe = self.pipe
        prompt = task["prompt"]
        # 统一命名前缀（文件库按 batch_ 分组）
        bname = f"batch_{idx + 1:03d}"

        # ── 断点续传：检查 .done 标记，跳过已完成任务 ──
        done_dir = Path(pipe.config["paths"]["output"]) / "batch_done"
        done_file = done_dir / f"{bname}.done"
        if done_file.exists():
            try:
                import json as _json
                info = _json.loads(done_file.read_text(encoding="utf-8"))
                task["img"] = info.get("img", "")
                task["glb"] = info.get("glb", "")
                task["stl"] = info.get("stl", "")
                task["status"] = "done"
                task["stage"] = ""
                pipe.state.selected_image_path = task["img"]
                pipe.state.current_glb_path = task["glb"]
                return  # 跳过
            except Exception:
                pass  # 标记文件损坏，重新执行

        # 1. 扩写提示词
        task["stage"] = "扩写中"
        enhanced = pipe.enhance_prompt(prompt)
        if not enhanced or len(enhanced) < 20:
            enhanced = prompt

        # 2. 生成 1 张图片（后端自动中译英）
        task["stage"] = "生成图片中"
        results = pipe.generate_images(
            prompt=enhanced, count=1,
            model_key="flux", quality_name="标准",
            resolution="1024×1024 (正方形)", seed=-1,
            negative=pipe.config["stages"]["image"].get("default_negative", ""),
        )
        ok = [r for r in results if r.success]
        if not ok:
            raise RuntimeError("图片生成失败")
        img_path = ok[0].output_path
        # 复制为 batch_ 命名
        import shutil
        img_out = Path(pipe.config["paths"]["output"]) / "images" / f"{bname}.png"
        shutil.copy2(img_path, img_out)
        task["img"] = str(img_out)
        pipe.state.selected_image_path = str(img_out)

        # 3. 生成 3D（ShapeGen-only）
        task["stage"] = "生成3D中"
        td = pipe.generate_3d(model_key="hunyuan3d", quality_name="快速")
        if not td.success:
            raise RuntimeError(f"3D 生成失败: {td.error[:150]}")
        glb_out = Path(pipe.config["paths"]["output"]) / "3d" / f"{bname}.glb"
        shutil.copy2(td.output_path, glb_out)
        task["glb"] = str(glb_out)

        # 4. 导出 STL
        task["stage"] = "导出STL中"
        stl = pipe.export_stl(td.output_path, target_size_mm=120, optimize_preset="fast")
        stl_out = None
        if stl and stl.success:
            stl_out = Path(pipe.config["paths"]["output"]) / "stl" / f"{bname}.stl"
            shutil.copy2(stl.output_path, stl_out)
            task["stl"] = str(stl_out)
        task["stage"] = ""

        # ── 写入断点续传标记 ──
        done_dir.mkdir(parents=True, exist_ok=True)
        import json
        done_file.write_text(json.dumps({
            "status": "done",
            "prompt": prompt,
            "img": task["img"],
            "glb": task["glb"],
            "stl": task["stl"] or "",
        }, ensure_ascii=False), encoding="utf-8")

    # ─── 状态渲染 ───────────────────────────────
    def status_html(self):
        with self._lock:
            tasks = list(self.tasks)
            cur = self.current_idx
        parts = [
            f'<div class="batch-header">'
            f'批量队列 {len(tasks)} 项 · '
            f'{"🟢 运行中" if self.running else "⏸ 空闲"}'
            f'</div>'
        ]
        # 当前任务实时进度条（2026-08-12 增强）
        if self.running and 0 <= cur < len(tasks):
            from backend.progress_tracker import get_progress_html
            bar = get_progress_html()
            if bar:
                parts.append(bar)
        for i, t in enumerate(tasks):
            icon = STATUS_ICONS.get(t["status"], "⏳")
            running = "batch-active" if (self.running and i == cur) else ""
            stage = t.get("stage", "")
            stage_txt = f'<span class="batch-stage">{stage}</span>' if stage else ""
            parts.append(
                f'<div class="batch-item {running}">'
                f'<span class="batch-ic">{icon}</span>'
                f'<span class="batch-idx">#{i+1}</span>'
                f'<span class="batch-prompt" title="{t["prompt"]}">{t["prompt"][:60]}</span>'
                f'{stage_txt}'
                f'<span class="batch-out">'
                f'{"🖼" if t["img"] else ""} {"🧊" if t["glb"] else ""} {"📦" if t["stl"] else ""}'
                f'</span>'
                f'<span class="batch-err">{t["error"][:80]}</span>'
                f'</div>'
            )
        if not tasks:
            parts.append('<div class="flib-empty">队列为空，在创作页添加提示词</div>')
        return "".join(parts)

    def stats(self):
        with self._lock:
            done = sum(1 for t in self.tasks if t["status"] == "done")
            failed = sum(1 for t in self.tasks if t["status"] == "failed")
            total = len(self.tasks)
        return total, done, failed
