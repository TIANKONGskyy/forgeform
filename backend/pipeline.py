"""Pipeline - 核心编排器，app.py 的全部后端接口"""
import json
import time
import shutil
import hashlib
import requests
import re
from pathlib import Path
from typing import Optional, Callable, List
from .logger import get_logger
from .env import normalize_config_paths
from .prompt_enhancer import enhance_local as llm_enhance, split_sections, is_ollama_running

logger = get_logger("pipeline")


def _release_chat_gpu() -> None:
    """生成前把对话大脑让出去。

    V100 32G 只能住一个：Strata 125B 约 29.5GB、Ollama 27B 约 19GB、
    ComfyUI 出图/建模也要十几 GB。provider=strata 时先停 Strata；
    如果对话正在回答，先等它答完（最多 30s），还忙就拒绝这次生成——
    不打断回答，也不让 ComfyUI 和 Strata 抢显存。
    """
    from backend import describer, gpu_state
    if describer._agent_cfg_light().get("provider") != "strata":
        return
    if gpu_state.is_chatting():
        print("[pipeline] 对话还在回答，等它结束后再让出显存…")
        if not gpu_state.wait_chat_idle(30):
            raise RuntimeError("对话还在回答，请等这一轮结束后再点生成")
        print("[pipeline] 对话已结束，开始生成")
    describer.strata_stop()


class PipelineState:
    """Pipeline 状态追踪"""
    
    def __init__(self):
        self.reset()
    
    def reset(self):
        self.image_paths = []
        self.selected_image_path = ""
        self.current_glb_path = ""
        self.current_video_path = ""
        self.steps_done = []
        self.steps_pending = ["image", "video", "3d", "stl"]
    
    def pipeline_html(self):
        """渲染 Pipeline 进度条 HTML（含 data 属性用于 CSS 动画）"""
        all_steps = [
            ("image", "🎨", "图片"),
            ("video", "🎬", "视频"),
            ("3d", "🧊", "3D"),
            ("stl", "📦", "STL"),
        ]
        parts = []
        for i, (key, icon, name) in enumerate(all_steps):
            active = key == self.steps_pending[0] if self.steps_pending else False
            done = key in self.steps_done
            cls = "step done" if done else ("step active" if active else "step")
            data_state = "done" if done else ("active" if active else "pending")
            parts.append(
                f'<div class="{cls}" data-state="{data_state}">'
                f'<span class="si">{icon}</span>'
                f'<span class="sn">{name}</span>'
                f'</div>'
            )
            if i < len(all_steps) - 1:
                conn_cls = "conn done" if done else "conn"
                parts.append(f'<div class="{conn_cls}"></div>')
        return '<div class="pipe" role="progressbar">' + ''.join(parts) + '</div>'


class Pipeline:
    """AI 创意工坊 Pipeline"""
    
    def __init__(self, config_path: str = None):
        # 加载配置
        if config_path is None:
            config_path = str(Path(__file__).parent.parent / "config.json")
        
        with open(config_path, "r", encoding="utf-8") as f:
            self.config = json.load(f)

        # paths.* 统一展开成绝对路径（相对值按仓库根解析，${COMFYUI_DIR} 按环境变量展开）。
        # config.example.json 只写相对值，所以这一步是"clone 下来就能跑"的关键。
        normalize_config_paths(self.config)
        
        # 兼容旧代码：如果 config 没有 stages 键，从 models 构建
        if "stages" not in self.config:
            self.config["stages"] = {}
            for stage_name, models in self.config.get("models", {}).items():
                self.config["stages"][stage_name] = {
                    "models": models,
                    "default_model": self.config.get("generation", {}).get(f"default_{stage_name}_model", "")
                }
        
        # 初始化生成后端。
        # 默认是 ComfyUI（config 里不写 backend 就完全走老路）；
        # 写 "backend": {"mode": "direct"} 就换成 diffusers / hy3dgen 直连，见 docs/BACKENDS.md。
        # 属性名仍叫 self.comfy —— 下面二十多处调用因此一行都不用改。
        from .backends import make_backend
        self.comfy = make_backend(self.config)
        self.backend_mode = getattr(self.comfy, "name", "comfyui")
        
        # 状态
        self.state = PipelineState()
        self._log_lines = []
        
        # 确保输出目录存在
        output_dir = Path(self.config["paths"]["output"])
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "images").mkdir(exist_ok=True)
        (output_dir / "videos").mkdir(exist_ok=True)
        (output_dir / "3d").mkdir(exist_ok=True)
        (output_dir / "stl").mkdir(exist_ok=True)
    
    # ─── 配置查询 ───────────────────────────────────────────
    
    def get_stage_models(self, stage_name: str) -> List[tuple]:
        """返回 [(key, display_name)]"""
        # 特殊名映射
        config_key = stage_name
        if stage_name == "threed":
            config_key = "3d"
        
        models = self.config.get("models", {}).get(config_key, {})
        result = []
        for key, m in models.items():
            if isinstance(m, dict):
                result.append((key, m.get("name", key)))
        return result if result else [(stage_name, stage_name)]
    
    def get_quality_presets(self) -> List[str]:
        """返回精度预设列表"""
        return ["快速", "标准", "高质量", "极致"]
    
    def get_resolutions(self) -> dict:
        """返回分辨率选项"""
        return {
            "1024×1024 (正方形)": (1024, 1024),
            "1344×768 (推荐)": (1344, 768),
            "768×1344 (竖版)": (768, 1344),
            "1536×640 (超宽)": (1536, 640),
            "512×512 (快速)": (512, 512),
        }
    
    # ─── 服务状态 ───────────────────────────────────────────
    
    def is_comfy_running(self) -> bool:
        return self.comfy.is_running()

    def cancel_current(self) -> bool:
        """中断当前 ComfyUI 任务（2026-08-13 新增）"""
        try:
            return self.comfy.interrupt()
        except Exception:
            return False
    
    def is_ollama_running(self) -> bool:
        return is_ollama_running()
    
    def get_queue(self) -> dict:
        """获取 ComfyUI 队列状态（按 config 端口聚合；单卡时 image/media 同端口，去重）"""
        running = pending = 0
        ports_cfg = self.config.get("comfyui_ports", {})
        img_port = ports_cfg.get("image", 8188)
        media_port = ports_cfg.get("media", img_port)
        for port in sorted({img_port, media_port}):
            try:
                r = requests.get(f"http://127.0.0.1:{port}/queue", timeout=5)
                if r.status_code == 200:
                    data = r.json()
                    running += len(data.get("queue_running", []))
                    pending += len(data.get("queue_pending", []))
            except Exception:
                pass
        return {"running": running, "pending": pending}
    
    # ─── 日志 ─────────────────────────────────────────────
    
    def _log(self, msg: str):
        ts = time.strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        self._log_lines.append(line)
        if len(self._log_lines) > 50:
            self._log_lines = self._log_lines[-50:]
        logger.info(msg)
    
    def get_log_text(self) -> str:
        return "\n".join(self._log_lines[-20:])
    
    # ─── 提示词扩写 ─────────────────────────────────────
    
    def enhance_prompt(self, prompt: str) -> str:
        """扩写提示词（IMAGE + 3D 双段）"""
        if not prompt or not prompt.strip():
            return prompt
        result = llm_enhance(prompt)
        if result and result != prompt:
            self._log(f"✨ 提示词扩写: {len(prompt)} → {len(result)} chars")
            return result
        return prompt

    def enhance_sections(self, prompt: str) -> dict:
        """扩写并拆分 IMAGE/3D 分段（2026-08-11：3D 用结构段，细节更好）
        返回 {"image": str, "three_d": str}
        """
        enhanced = self.enhance_prompt(prompt)
        sections = split_sections(enhanced)
        if not sections["three_d"]:
            sections["three_d"] = enhanced  # 兼容旧模型输出
        return sections
    
    # ─── 图片生成 ─────────────────────────────────────────
    
    def _ensure_english(self, prompt: str) -> str:
        """FLUX T5 对中文理解差（实测中文直调会主题漂移，生成无关内容）。
        检测到中文自动翻译成英文再提交（2026-08-12 VLM 排查发现）
        """
        if prompt and re.search(r'[\u4e00-\u9fff]', prompt):
            from backend.prompt_enhancer import translate
            en = translate(prompt, "en")
            if en:
                self._log("🌐 中文提示词已自动翻译为英文")
                return en
        return prompt

    def generate_images(self, prompt: str, count: int = 1,
                       model_key: str = "flux", quality_name: str = "标准",
                       resolution: str = "1024×1024", seed: int = -1,
                       negative: str = "", progress_cb: Callable = None,
                       skip_cache: bool = False,
                       steps_override: int = 0,
                       reference_image: str = None) -> List:
        """生成图片，返回 StageResult 列表。

        steps_override：>0 时覆盖质量档位计算的步数，让 UI 步数滑块生效。
        reference_image：给了就**必须**走 Qwen-Image-2.1 参考图工作流（"照图生成指定角色"的唯一
            可靠路径 —— IP 角色纯文字生图只能猜 ✗）。会先上传到 ComfyUI 的 input 目录。
        """
        from .stages.base import StageResult

        _release_chat_gpu()
        # FLUX 中文漂移修复
        prompt = self._ensure_english(prompt)
        if "【IMAGE】" in prompt:
            sections = split_sections(prompt)
            if sections.get("image"):
                prompt = sections["image"]

        # ★★ 2026-10-15 加：把**真正提交给模型**的提示词打出来 ★★
        #
        # 为什么必须打这一行：用户的报障是"要浣熊/大象，出的是卡通小人"，
        # 而界面上显示的英文扩写**一直是对的** —— 光看扩写日志永远分不清
        # "扩写写错了" 还是 "送进去的根本不是它"。实测这两种情况长得一模一样：
        #   · 日志有 "✨ 生图提示词扩写+英译: 17→138 字符"（扩写正确、长度也合理）
        #   · 但出图完全跑题
        # 只有把最终交给 ComfyUI 的那串字打出来，才能一眼定位是上游写错、
        # 中途被替换、还是这里读到的压根是另一个变量。
        #
        # 注意：这里已经是**最终值**（翻译 + 分节提取都做完了），
        # 所以它和 CLIPTextEncode 里收到的文本是一致的。
        # 注意：用 print 不用 self._log —— 实测 self._log 不写到这个 stdout，
        # 加了等于没加（排查时会被误导成"这行代码没执行"）。print 才真的落盘。

        # ══════════════════════════════════════════════════════════
        # ★★ 最后一道兜底：否定词清除 + 单物体强制（2026-10-17）★★
        #
        # 为什么必须放在**这里**（而不是只在 describer 里）：
        #   实测发现有路径**绕过** _clean_image_prompt 直接把字符串送到这里 ——
        #   「带洞洞的拖鞋」最后提交的是 "Slippers with holes..."（复数），
        #   说明上游某条分支没走后处理，规则写了却不生效 ✗
        #   放在真正提交前这一行，无论上游几条路径，都一定被清一遍 ✓
        #
        # 为什么在 print 之前：
        #   打出来的日志就是**真正送进模型的那串字**，
        #   否则日志和实际不一致，下次排查又会被误导。
        # ══════════════════════════════════════════════════════════
        try:
            from .describer import _ensure_clean_background, _strip_negations
            _before = prompt
            prompt = _strip_negations(prompt)
            if prompt != _before:
                print(f"[prompt] 🧹 否定词/单物体兜底: {_before[:80]} → {prompt[:80]}")
            # 补干净背景（必须在 _strip_negations 之后：它会把 "no shadow" 删掉）。
            # 参考图的灰底+投影是 2mini 重建出"底部地毯"的根因，见 docs/MODEL-BENCH.md。
            _bg_before = prompt
            prompt = _ensure_clean_background(prompt)
            if prompt != _bg_before:
                print(f"[prompt] 🧼 补干净背景: +{len(prompt) - len(_bg_before)} 字符")
        except Exception as _e:
            print(f"[prompt] ⚠️ 兜底清理异常（忽略）: {_e!r}")

        print(f"[prompt] 实际提交给模型({len(prompt)}字符): {prompt[:300]}")

        # 负向提示词回退
        if not negative:
            negative = self.config.get("stages", {}).get("image", {}).get("default_negative", "")

        # 解析分辨率
        res_map = self.get_resolutions()
        if resolution in res_map:
            width, height = res_map[resolution]
        else:
            width, height = 1024, 1024

        # 模型配置
        img_models = self.config.get("models", {}).get("image", {})
        model_cfg = img_models.get(model_key, list(img_models.values())[0] if img_models else {})

        # ── 参考图分支（2026-09-23 新增）──────────────────────────────
        # 有参考图时**必须**用 Qwen-Image-2.1 的参考图工作流：纯文字生图对 IP 角色只能猜 ✗
        # （实测 Z-Image 6B / FLUX.2 Klein 9B 都出"标准卡通棕熊"而不是熊大）。
        # 这一步要把图上传到 ComfyUI 的 input 目录，LoadImage 才找得到。
        ref_name = ""
        if reference_image:
            if not img_models.get("qwen21_ref"):
                raise RuntimeError("config.json 里缺 models.image.qwen21_ref（参考图生图配置）")
            model_cfg = img_models["qwen21_ref"]
            model_key = "qwen21_ref"
            steps = int(model_cfg.get("steps", 20))          # 参考图分支不套质量倍率（会掉步数）
            ref_name = self.comfy.upload_image(reference_image, stage="image")
            self._log(f"🖼 参考图已上传 ComfyUI: {ref_name} → Qwen-Image-2.1 参考图工作流")

        # 质量 → 步数 / guidance
        step_mult = {"快速": 0.5, "标准": 1.0, "高质量": 1.5, "极致": 2.0}
        base_steps = model_cfg.get("steps", 20)
        steps = int(base_steps * step_mult.get(quality_name, 1.0))
        if steps_override and steps_override > 0:
            steps = int(steps_override)
        if reference_image:
            steps = int(model_cfg.get("steps", 20))       # 参考图分支固定用模型自带步数

        quality_guidance = {"快速": 3.0, "标准": 3.5, "高质量": 3.5, "极致": 4.0}
        guidance = quality_guidance.get(quality_name, 3.5)

        # 缓存
        cache_dir = Path(self.config["paths"]["output"]) / ".cache" / "images"
        cache_dir.mkdir(parents=True, exist_ok=True)

        results = []
        for i in range(count):
            if progress_cb:
                progress_cb(f"生成第 {i+1}/{count} 张...")

            img_seed = seed + i if seed > 0 else -1
            self._log(f"🎨 生成图片 {i+1}/{count}: {model_key} {width}x{height} {steps}步")

            # 缓存命中
            cache_key = None
            if img_seed > 0 and not skip_cache:
                cache_key = hashlib.md5(json.dumps({
                    "prompt": prompt, "negative": negative,
                    "model": model_key, "resolution": f"{width}x{height}",
                    "quality": quality_name, "steps": steps, "seed": img_seed,
                    "teacache_thresh": model_cfg.get("teacache_thresh", 0),
                    "teacache_enabled": model_cfg.get("teacache", False),
                }, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
                cached = cache_dir / f"{cache_key}.png"
                if cached.exists():
                    self.state.image_paths.append(str(cached))
                    self._log(f"⚡ 命中缓存: {cached.name}")
                    results.append(StageResult(True, str(cached), metadata={
                        "seed": img_seed, "cached": True,
                        # ★ 2026-10-15：把真实参数带回去。UI 那句「（qwen21_ref 8步 turbo）」
                        #   原来是**写死的字面量**，和实际用的模型/步数/分辨率毫无关系
                        #   （实测：显示 8 步，而 qwen21_ref 配置是 20 步、且预览只跑 512）。
                        #   用户拿这个判断"到底细不细"，编的数字等于骗人。
                        "model": model_key, "steps": steps,
                        "resolution": f"{width}x{height}", "quality": quality_name,
                    }))
                    continue

            params = {
                "model_config": model_cfg,
                "prompt": prompt,
                "negative_prompt": negative,
                "width": width,
                "height": height,
                "steps": steps,
                "seed": img_seed,
                "guidance": guidance,
            }
            if ref_name:
                params["reference_image"] = ref_name
                params["resolution_px"] = max(width, height)   # 2.1 的 TextEncodeQwenImage21 要 INT

            # GPU 降温检查（每张生成前查一次）
            self._wait_gpu_cool()

            try:
                workflow_data = self.comfy.build_workflow("image", params)
                prompt_id = self.comfy.submit_workflow(workflow_data, stage="image")
                self._log(f"任务已提交: {prompt_id[:8]}...")

                # 等待完成（带温度看门狗）
                result = self.comfy.wait_for_completion(
                    prompt_id, timeout=600, progress_callback=progress_cb, thermal_guard=True)
                outputs = result.get("outputs", {})

                image_url = self.comfy.get_result_image(outputs, stage="image")
                if image_url:
                    output_path = Path(self.config["paths"]["output"]) / "images" / f"img_{int(time.time())}_{i}.png"
                    resp = requests.get(image_url, timeout=60)
                    if resp.status_code == 200:
                        with open(output_path, "wb") as f:
                            f.write(resp.content)
                        if cache_key:
                            try:
                                shutil.copy2(output_path, cache_dir / f"{cache_key}.png")
                            except Exception:
                                pass
                        self.state.image_paths.append(str(output_path))
                        self._log(f"✅ 图片已保存: {output_path}")
                        results.append(StageResult(True, str(output_path), metadata={
                            "seed": img_seed,
                            # ★ 2026-10-15：同上面缓存分支 —— 把真实参数带回去给 UI 显示
                            "model": model_key, "steps": steps,
                            "resolution": f"{width}x{height}", "quality": quality_name,
                        }))
                    else:
                        results.append(StageResult(False, error="下载图片失败"))
                else:
                    results.append(StageResult(False, error="ComfyUI 未返回图片"))

            except Exception as e:
                self._log(f"❌ 图片生成失败: {e}")
                results.append(StageResult(False, error=str(e)))

        return results
    
    def select_image(self, idx: int) -> str:
        """选择图片"""
        if 0 <= idx < len(self.state.image_paths):
            path = self.state.image_paths[idx]
            self.state.selected_image_path = path
            self._log(f"选中图片 #{idx}: {path}")
            return path
        return ""

    # ─── 高清重绘（2026-08-12：低精度候选 → 选中后 img2img 提精度）───

    def enhance_image(self, image_path: str, prompt: str, model_key: str = "flux",
                      quality_name: str = "标准", resolution: str = "1024×1024",
                      negative: str = "", seed: int = -1, denoise: float = 0.5,
                      progress_cb: Callable = None, steps_override: int = 0) -> str:
        """img2img 高清重绘：把候选低精度图提升到目标分辨率/质量
        返回新图路径，失败返回空字符串
        steps_override（2026-08-14）：>0 时覆盖质量档位步数，让步数滑块对高清重绘也生效
        """
        from .stages.base import StageResult  # noqa
        import shutil
        _release_chat_gpu()          # 重绘也要显存：先停 Strata，否则 img2img 会 OOM 出白图
        # FLUX 中文漂移修复
        prompt = self._ensure_english(prompt)
        try:
            # 1. 解析分辨率
            res_map = self.get_resolutions()
            if resolution in res_map:
                width, height = res_map[resolution]
            else:
                width, height = 1024, 1024

            # 2. 模型配置
            img_models = self.config.get("models", {}).get("image", {})
            model_cfg = img_models.get(model_key, list(img_models.values())[0] if img_models else {})

            # 3. 质量 → 步数
            step_mult = {"快速": 0.5, "标准": 1.0, "高质量": 1.5, "极致": 2.0}
            base_steps = model_cfg.get("steps", 20)
            steps = int(base_steps * step_mult.get(quality_name, 1.0))
            steps = max(steps, 8)
            # 步数手动覆盖（UI 步数滑块）
            if steps_override and steps_override > 0:
                steps = max(int(steps_override), 8)

            # 4. 候选图复制到 ComfyUI input 目录（LoadImage 只认 input 下的文件名）
            if not image_path or not Path(image_path).exists():
                self._log("❌ 高清重绘：候选图不存在")
                return ""
            input_dir = Path(self.config["paths"]["comfyui"]) / "input"
            input_dir.mkdir(parents=True, exist_ok=True)
            in_name = f"hd_src_{int(time.time())}.png"
            shutil.copy2(image_path, input_dir / in_name)

            # 5. 构建 img2img 工作流
            img_seed = seed if seed > 0 else -1
            params = {
                "model_config": model_cfg,
                "prompt": prompt,
                "negative_prompt": negative,
                "width": width,
                "height": height,
                "steps": steps,
                "seed": img_seed,
                "init_image": in_name,
                "denoise": denoise,
            }
            self._log(f"🔍 高清重绘: {width}x{height} {steps}步 denoise={denoise}")
            # [patch 2026-08-18] 重绘前等 GPU 降温（83°C 墙）
            self._wait_gpu_cool()
            workflow_data = self.comfy.build_workflow("image", params)

            # 6. 提交 + 等待 + 下载
            prompt_id = self.comfy.submit_workflow(workflow_data)
            self._log(f"高清任务已提交: {prompt_id[:8]}...")
            result = self.comfy.wait_for_completion(prompt_id, timeout=600, progress_callback=progress_cb, thermal_guard=True)
            outputs = result.get("outputs", {})
            image_url = self.comfy.get_result_image(outputs)
            if image_url:
                output_path = Path(self.config["paths"]["output"]) / "images" / f"hd_{int(time.time())}.png"
                resp = requests.get(image_url, timeout=60)
                if resp.status_code == 200:
                    with open(output_path, "wb") as f:
                        f.write(resp.content)
                    self._log(f"✅ 高清图已保存: {output_path}")
                    # OOM 时 ComfyUI 可能返回一张几乎纯色的图（实测 15:14 那张米白方块），
                    # 它会被当成"重绘成功"喂给 Hunyuan → 建出一块平板。纯色一律按失败处理。
                    try:
                        from PIL import Image
                        import numpy as np
                        arr = np.asarray(Image.open(output_path).convert("RGB"), dtype=np.float32)
                        std = float(arr.std())
                        if std < 6.0:
                            self._log(f"⚠️ 高清图几乎是纯色（std={std:.1f}），按失败处理")
                            output_path.unlink(missing_ok=True)
                            return ""
                    except Exception:
                        pass
                    return str(output_path)
                else:
                    self._log("❌ 高清图下载失败")
                    return ""
            else:
                self._log("❌ ComfyUI 未返回高清图")
                return ""
        except Exception as e:
            self._log(f"❌ 高清重绘失败: {e}")
            return ""

    # ─── 定向修改·局部重绘（2026-08-12：涂抹勾画 + inpaint）───

    def inpaint_image(self, image_path: str, mask_path: str, prompt: str,
                      model_key: str = "flux", quality_name: str = "标准",
                      resolution: str = "1024×1024", negative: str = "",
                      seed: int = -1, denoise: float = 1.0,
                      progress_cb: Callable = None) -> str:
        """局部重绘：mask 白色区域按 prompt 重绘，其余保持
        返回新图路径，失败返回空字符串
        """
        import shutil
        # FLUX 中文漂移修复
        prompt = self._ensure_english(prompt)
        try:
            res_map = self.get_resolutions()
            if resolution in res_map:
                width, height = res_map[resolution]
            else:
                width, height = 1024, 1024

            img_models = self.config.get("models", {}).get("image", {})
            model_cfg = img_models.get(model_key, list(img_models.values())[0] if img_models else {})

            step_mult = {"快速": 0.5, "标准": 1.0, "高质量": 1.5, "极致": 2.0}
            base_steps = model_cfg.get("steps", 20)
            steps = int(base_steps * step_mult.get(quality_name, 1.0))
            steps = max(steps, 8)

            if not image_path or not Path(image_path).exists() or not mask_path or not Path(mask_path).exists():
                self._log("❌ 定向修改：原图或遮罩不存在")
                return ""
            input_dir = Path(self.config["paths"]["comfyui"]) / "input"
            input_dir.mkdir(parents=True, exist_ok=True)
            ts = int(time.time())
            in_img = f"ip_src_{ts}.png"
            in_mask = f"ip_mask_{ts}.png"
            shutil.copy2(image_path, input_dir / in_img)
            shutil.copy2(mask_path, input_dir / in_mask)

            img_seed = seed if seed > 0 else -1
            params = {
                "model_config": model_cfg,
                "prompt": prompt,
                "negative_prompt": negative,
                "width": width,
                "height": height,
                "steps": steps,
                "seed": img_seed,
                "init_image": in_img,
                "mask_file": in_mask,
                "denoise": denoise,
            }
            self._log(f"🛠 局部重绘: {width}x{height} {steps}步 mask={in_mask}")
            # [patch 2026-08-18] 局部重绘前等 GPU 降温（83°C 墙）
            self._wait_gpu_cool()
            workflow_data = self.comfy.build_workflow("image", params)

            prompt_id = self.comfy.submit_workflow(workflow_data)
            self._log(f"局部重绘已提交: {prompt_id[:8]}...")
            result = self.comfy.wait_for_completion(prompt_id, timeout=600, progress_callback=progress_cb, thermal_guard=True)
            outputs = result.get("outputs", {})
            image_url = self.comfy.get_result_image(outputs)
            if image_url:
                output_path = Path(self.config["paths"]["output"]) / "images" / f"edit_{ts}.png"
                resp = requests.get(image_url, timeout=60)
                if resp.status_code == 200:
                    with open(output_path, "wb") as f:
                        f.write(resp.content)
                    self._log(f"✅ 修改结果已保存: {output_path}")
                    return str(output_path)
                else:
                    self._log("❌ 修改结果下载失败")
                    return ""
            else:
                self._log("❌ ComfyUI 未返回修改结果")
                return ""
        except Exception as e:
            self._log(f"❌ 定向修改失败: {e}")
            return ""
    
    # ─── 视频生成 ─────────────────────────────────────────
    
    def generate_video(self, model_key: str = "h3", quality_name: str = "标准",
                       prompt: str = "", resolution=None) -> object:
        """从选中图片生成视频（prompt/resolution 可选覆盖）。"""
        from .stages.base import StageResult
        _release_chat_gpu()
        
        input_image = self.state.selected_image_path
        if not input_image or not Path(input_image).exists():
            return StageResult(False, error="未选择图片或图片不存在")
        
        # 获取模型配置
        vid_models = self.config.get("models", {}).get("video", {})
        model_cfg = dict(vid_models.get(model_key, list(vid_models.values())[0] if vid_models else {}))
        if resolution:
            model_cfg["resolution"] = list(resolution)
        
        self._log(f"🎬 视频生成: {model_key}")
        
        # 释放 VRAM（视频生成需要大量显存）
        self._free_vram()
        
        # [patch 2026-08-18] 视频生成前紧急冗余（90°C 才等；渐进降频由 thermal_governor 负责）
        self._wait_gpu_cool()
        
        # 上传图片到 ComfyUI（video 阶段 → 8189 实例/GPU1）
        try:
            uploaded_name = self.comfy.upload_image(input_image, stage="video")
        except Exception as e:
            return StageResult(False, error=f"上传图片失败: {e}")
        
        params = {
            "model_config": model_cfg,
            "input_image": uploaded_name,
            "image_path": str(input_image),
            "prompt": prompt or getattr(self.state, "base_prompt", "") or getattr(self.state, "user_prompt", "") or "",
            "seed": -1,
        }
        # 精度档接线（2026-08-14 调研：6-8 步最优，turbo 强度官方调教点 1.0）
        vid_quality = {
            "快速": {"steps": 4, "turbo": True},       # turbo 4 步，2x 快，草稿
            "标准": {"steps": 6, "turbo": True},       # turbo 6 步，官方 6-8 步甜点
            "高质量": {"steps": 8, "turbo": False},    # 8 步无 turbo，画质优先
            "极致": {"steps": 10, "turbo": False},     # 10 步，极限画质
        }
        q = vid_quality.get(quality_name, {"steps": 6, "turbo": True})
        params["steps"] = q["steps"]
        params["turbo_enabled"] = q["turbo"]
        self._log(f"🎬 视频精度档: {quality_name} → {q['steps']}步 turbo={q['turbo']}")
        
        try:
            workflow_data = self.comfy.build_workflow("video", params)
            prompt_id = self.comfy.submit_workflow(workflow_data, stage="video")
            self._log(f"视频任务已提交: {prompt_id[:8]}...")
            
            result = self.comfy.wait_for_completion(prompt_id, timeout=1800, thermal_guard=True)
            outputs = result.get("outputs", {})
            
            video_url = self.comfy.get_result_video(outputs, stage="video")
            if video_url:
                output_path = Path(self.config["paths"]["output"]) / "videos" / f"vid_{int(time.time())}.mp4"
                resp = requests.get(video_url, timeout=120)
                if resp.status_code == 200:
                    with open(output_path, "wb") as f:
                        f.write(resp.content)
                    self.state.current_video_path = str(output_path)
                    self.state.steps_done.append("video")
                    self._log(f"✅ 视频已保存: {output_path}")
                    return StageResult(True, str(output_path))
            
            return StageResult(False, error="ComfyUI 未返回视频")
            
        except Exception as e:
            self._log(f"❌ 视频生成失败: {e}")
            return StageResult(False, error=str(e))
    
    # ─── 3D 产物质量闸 ────────────────────────────────────

    def _mesh_parts(self, path: str) -> tuple:
        """(连通块数, 面数)；读不动返回 (-1, -1)"""
        try:
            import trimesh
            m = trimesh.load(path, force="mesh")
            return len(m.split(only_watertight=False)), len(m.faces)
        except Exception:
            return -1, -1

    def _mesh_is_flat_slab(self, path: str) -> tuple:
        """是不是"物体 + 一大片背景板"？

        2026-09-23 实测：Hunyuan ShapeGen 设成不去背景时，产物 = 小熊形体 + 一片碎片板，
        而且板与熊连成一个连通块 → 只数连通块的闸门被骗过（1 块 / 4 万面，看着"通过"）。
        这里补一条几何判定：包围盒最薄的那一边远小于最长边 → 判为夹带了背景板。

        ★ 2026-10-17 收紧：原判据 `最薄 < 5% 最长` 会**漏判**。
          实测菠萝兜底产物 extents=[0.24, 1.33, 1.99]：
            最薄 0.24 / 最长 1.99 = 12% → 大于 5% → 判"不薄"→ 通过 ✗
          但它肉眼就是"一块带碎片的板"：物体本身有厚度(0.24)，
          背后贴的碎片板把包围盒撑成了 8:1，于是躲过了 5% 的线。

        ★★ 我试过两条"更聪明"的几何判据，**都被实测数据否掉了**，别再走回头路：
            ① 顶点在最薄轴上的集中度（认为板 = 顶点挤在一薄层）
               实测：菠萝最薄2%层 2.5% 顶点，钢铁侠 1.0% —— 差别太小，
                     设任何阈值都会把正常模型一起拦下 ✗
            ② 体积填充率（认为板 = 填充率低）
               实测：菠萝 54.3%、钢铁侠 13.4%、拖鞋 6.0% —— 方向**完全相反**，
                     菠萝反而最"实心" ✗
           结论：一个"贴着碎片的薄板"在**包围盒/体积/顶点分布**上都可能
                 长得像正常模型。纯几何判据区分不了。
                 目前只保留确定有效的 5% 极端薄片判据。
                 更好的办法是看**渲染轮廓**或让用户在预览图上确认，
                 那需要视觉模型/人工，不在这个函数的能力范围内。
        """
        try:
            import trimesh
            m = trimesh.load(path, force="mesh")
            ext = sorted(float(x) for x in m.extents)
            if not ext or ext[-1] <= 0:
                return False, "无法量尺寸"
            ratio = ext[-1] / max(ext[0], 1e-9)
            flat = ext[0] < 0.05 * ext[-1] and len(m.faces) > 5000
            return flat, f"包围盒 {[round(x, 1) for x in ext]} 最长/最薄={ratio:.0f}"
        except Exception:
            return False, "无法判定"

    def _finalize_3d(self, output_path: str, model_key: str, quality_name: str,
                     input_image: str, fallback_tried: bool,
                     max_facenum: int = 0, octree_override: int = 0) -> object:
        """3D 产物出厂前的质量闸 + 自动兜底。

        实测（2026-09-23）：TripoSplat 采样参数不对时，8 组产物全是 2000~15500 个连通块的
        "碎块云"、无一水密——渲染出来是漂浮碎块而非模型，但流程会一路当成成功（随后
        mesh_optimizer 把它糊成一个石头状块体，用户看到的就是"建模有大问题"）。
        闸门：碎块云不许出厂，自动改用 HunyuanD3D 2.1（VoxelToMesh surface-net 出水密单体）
        重建一次，两个里挑连通块少的那个。
        """
        from .stages.base import StageResult

        # ★ 2026-11-13：先试着把 2mini 重建出来的"底部背景板"切掉，再做质量闸。
        #   参考图是产品摄影风格（灰底 + 地面阴影）时，2mini 会把背景和阴影
        #   一起重建成几何，模型底下多一块带纹理的平板。实测四件艺术品全中
        #   （见 outputs/art/art_inputs.png）。
        #   切法是纯几何判断：最底层三角形占比 > 12% 视为"盘子"，沿转折处切。
        #   切完再判质量，能救回一批本来会被判碎块云的产物。
        try:
            from tools.trim_base import trim as _trim_base
            import tempfile as _tf
            _tmp = _tf.NamedTemporaryFile(suffix=".glb", delete=False).name
            _info = _trim_base(output_path, _tmp, dry=False)
            if _info.get("cut_z") is not None and _info.get("faces_out", 0) > 100:
                import shutil as _sh
                _sh.move(_tmp, output_path)
                self._log(f"🧹 已切掉底部背景板: 面数 {_info['faces_in']:,} → "
                          f"{_info['faces_out']:,}（小 -{_info['removed_pct']}%）")
            else:
                Path(_tmp).unlink(missing_ok=True)
        except Exception as _e:
            self._log(f"[trim] 跳过（{type(_e).__name__}: {_e}）")

        parts, faces = self._mesh_parts(output_path)
        flat, flat_msg = self._mesh_is_flat_slab(output_path)
        bad = parts < 0 or parts > 200 or faces < 100 or flat
        why = f"{parts} 个连通块 / {faces} 面"
        if flat:
            why += f" / 疑似夹带背景板（{flat_msg}）"
        self._log(f"🧊 3D 质量闸: {why} {'→ 不合格' if bad else '→ 通过'}")
        if not bad:
            return StageResult(True, output_path)
        if fallback_tried:
            return StageResult(False, error=f"3D 网格质量检查未通过：{why}")

        self._log(f"⚠️ 重建结果是碎块云（{parts} 块），自动改用 Hunyuan3D 2.1 重新生成…")
        retry = self.generate_3d(model_key="hunyuan21", quality_name=quality_name,
                                 image_path=input_image, _fallback_tried=True,
                                 # ★ 2026-10-18：重试必须继承精度参数，
                                 #   否则第一次按 4 万面出、重试又变回 20 万面，
                                 #   前后不一致（实测 C 档就是这样：设了 4 万，出来 20 万）
                                 max_facenum=max_facenum, octree_override=octree_override)
        if retry and getattr(retry, "success", False) and getattr(retry, "output_path", ""):
            rp, rf = self._mesh_parts(retry.output_path)
            rflat, _ = self._mesh_is_flat_slab(retry.output_path)
            rbad = rp < 0 or rp > 200 or rf < 100 or rflat
            self._log(f"🧊 兜底结果: {rp} 个连通块 / {rf} 面"
                      f"{'（夹带背景板）' if rflat else ''}")
            # 取舍不能只比连通块数：原产物可能是"1 块但夹带背景板"（板与物体连成一体），
            # 兜底也是 1 块 → 一比就误留坏的、把好的丢了（我第一版就踩了这个）。
            # 仅采用合格兜底；两个都不合格时停止，不能拿较少碎片冒充成功。
            adopt = not rbad
            if adopt:
                self.state.current_glb_path = retry.output_path
                self._log(f"✅ 采用兜底模型（原 {parts} 块{'/夹带背景板' if flat else ''} "
                          f"→ 兜底 {rp} 块{'' if not rflat else '/夹带背景板'}）: {retry.output_path}")
                return StageResult(True, retry.output_path)
            self._log(f"⚠️ 兜底网格仍不合格（{rp} 块），停止导出")
        else:
            self._log("⚠️ 兜底重建失败，停止导出")
        return StageResult(False, error=f"3D 网格质量检查未通过，重试也未修复：{why}")

    # ─── 3D 生成 ─────────────────────────────────────────
    
    def generate_3d(self, model_key: str = "hunyuan3d", quality_name: str = "标准",
                    image_path: str = None, _fallback_tried: bool = False,
                    max_facenum: int = 0, octree_override: int = 0) -> object:
        """从选中图片生成 3D 模型

        _fallback_tried 由质量闸内部使用：碎片产物自动改用 Hunyuan3D 2.1 重来时防无限递归。

        ★ 2026-10-18 新增 max_facenum / octree_override：
          质量档（quality_name）是给用户选的**预设**；这两个参数是给
          "精度对比测试 / 终稿精确控制"用的**直通旋钮** —— 传了就用传的，
          不传仍走 quality_name 的预设（不改变原有行为）。
        """
        from .stages.base import StageResult
        _release_chat_gpu()
        
        input_image = image_path or self.state.selected_image_path
        if not input_image or not Path(input_image).exists():
            return StageResult(False, error="未选择图片或图片不存在")

        # ★ 2026-11-13：建模前先处理参考图（Meshy / Tripo / 混元都这么做）
        #   ① rembg 去背景 —— 背景会被当成几何重建，实测模型底下会多一块平板
        #   ② 按 alpha 紧裁 —— 主体占满画面，同样的分辨率等于更多细节
        #   ③ 锐化 —— octree 靠图像特征推轮廓，边缘糊则轮廓糊
        #   失败不阻断（降级用原图），只记一行日志。
        try:
            from .preprocess import prepare as _prep
            _pin = Path(input_image)
            # 已经是预处理过的图就跳过，否则会出 boy_ref_prep_prep.png 这种套娃
            if _pin.stem.endswith("_prep"):
                self._log("🖼 参考图已预处理过，跳过")
                _pout = _pin
                _r = {"ok": False}
            else:
                _pout = _pin.with_name(_pin.stem + "_prep.png")
                _r = _prep(str(_pin), out_path=str(_pout))
            if _r.get("ok"):
                input_image = _r["path"]
                self._log(f"🖼 参考图预处理: {' · '.join(_r['steps'])}")
            else:
                self._log(f"🖼 参考图预处理跳过: {_r.get('error', '未知')}")
        except Exception as _e:
            self._log(f"🖼 参考图预处理跳过（{type(_e).__name__}）")
        
        # 获取模型配置
        td_models = self.config.get("models", {}).get("3d", {})
        model_cfg = td_models.get(model_key)
        if not model_cfg:
            # 不能静默取"第一个模型"：config 里写了不存在的 key 时（例如 fallback_chain 里的
            # "hunyuan3d"），原来会悄悄用第一个（=triposplat）→ 兜底兜回坏路径还看不出来。
            model_cfg = list(td_models.values())[0] if td_models else {}
            self._log(f"⚠️ 3D 模型 key '{model_key}' 不存在 → 回退到 "
                      f"'{model_cfg.get('name', '未知')}'（请检查 config 里的 key）")
        
        self._log(f"🧊 3D 生成: {model_key}")
        
        # 释放 VRAM（3D 生成需要大量显存）
        self._free_vram()
        
        # [patch 2026-08-18] 等 GPU 降温再跑 3D（90°C 才紧急等；渐进降频由 thermal_governor 负责）
        self._wait_gpu_cool()
        
        # 上传图片到 ComfyUI（3d 阶段 → 8189 实例/GPU1）
        #
        # ★ 2026-11-13 修：直连后端（hy3dgen）吃的是**本地绝对路径**，不是 ComfyUI
        #   上传后的文件名。原来无条件 upload 并把结果塞进 params["input_image"]，
        #   而 direct.py 读的是 params["image_path"] → 永远是空串，每次都报
        #   "输入图片不存在"，直连建模从来没被真正调通过。
        uploaded_name = ""
        try:
            uploaded_name = self.comfy.upload_image(input_image, stage="3d")
        except Exception as e:
            if self.backend_mode != "direct":
                return StageResult(False, error=f"上传图片失败: {e}")
            self._log(f"[直连] ComfyUI 上传失败，忽略: {e}")
        
        output_path = str(Path(self.config["paths"]["output"]) / "3d" / f"model_{int(time.time())}.glb")
        
        params = {
            "model_config": model_cfg,
            "model_key": model_key,   # 后端靠它选模型；缺了会落到 stages.default_model
            "input_image": uploaded_name,
            "image_path": str(input_image),   # 直连后端读这个（ComfyUI 后端读 input_image）
            "output_path": output_path,
            "seed": -1,
            # TexGen 需要 custom_rasterizer CUDA 扩展编译（缺 nvcc+MSVC），默认 ShapeGen-only
            "texgen": False,
        }
        # 精度档接线（2026-08-14 调研：octree 日常 384/终稿 512，guidance 5.0 基线）
        # 2026-08-20 宇凯要求细节翻倍：标准档 384→512（双卡 40GB 显存充裕）
        #
        # 【2026-09-23 修】原来只设 octree_resolution/guidance_scale，而 TripoSplat 的工作流
        # **根本不读这两个**（硬编码 resolution=384 / cfg=5.0 / sampler=euler）→ 精度档对 splat
        # 路径完全无效，日志还照报"octree=512 guidance=5.0"（骗人）。现在按模型分流：
        is_tripo = "tripo" in str(model_key).lower() or "tripo" in str(model_cfg.get("name", "")).lower()
        if is_tripo:
            # 官方蓝图 blueprints/Image to Gaussian Splat (TripoSplat).json 的基准：
            # 20 步 / cfg 3 / dpmpp_2m / resolution 384（"标准"档就是这套官方值）。
            tp_ladder = {
                "快速":   {"steps": 20, "cfg": 3.0, "sampler": "dpmpp_2m", "resolution": 256,
                           "smooth": 0,  "min_component": 500},
                "标准":   {"steps": 20, "cfg": 3.0, "sampler": "dpmpp_2m", "resolution": 384,
                           "smooth": 0,  "min_component": 500},     # = 官方蓝图原值
                "高质量": {"steps": 25, "cfg": 3.0, "sampler": "dpmpp_2m", "resolution": 512,
                           "smooth": 8,  "min_component": 900},     # 细网格 + Taubin 平滑
                "极致":   {"steps": 30, "cfg": 3.0, "sampler": "dpmpp_2m", "resolution": 640,
                           "smooth": 16, "min_component": 1200},
            }
            params["_tripo_quality"] = tp_ladder.get(quality_name, tp_ladder["标准"])
            q = params["_tripo_quality"]
            self._log(f"🧊 3D 精度档(TripoSplat): {quality_name} → {q['steps']}步 "
                      f"cfg={q['cfg']} {q['sampler']} 网格{q['resolution']} "
                      f"平滑{q['smooth']} 剔碎{q['min_component']}")
        else:
            td_quality = {
                # ★ 2026-10-18 宇凯反馈"建模精度太低" —— 三档原来**全是 512**，
                #   只是 guidance 不同（5/6/7），等于没有精度差。现在按 octree 拉开：
                #     octree 每翻一倍，网格顶点数约翻 8 倍（体积分辨率），
                #     细节（薄壁、小孔、锐边）才真的更清楚。
                "快速": {"octree_resolution": 256, "guidance_scale": 5.0},   # ~50K 顶点，预览用
                "标准": {"octree_resolution": 384, "guidance_scale": 5.0},   # 日常
                "高质量": {"octree_resolution": 512, "guidance_scale": 6.0}, # 原来"标准"就是这档
                "极致": {"octree_resolution": 768, "guidance_scale": 7.0},   # 细件加档
                # 「最高精度」= 实验档：1024。显存与耗时都显著上升，只在明确要终稿时用。
                "最高精度": {"octree_resolution": 1024, "guidance_scale": 8.0},
            }
            q = td_quality.get(quality_name, {"octree_resolution": 512, "guidance_scale": 5.0})
            params["octree_resolution"] = q["octree_resolution"]
            params["guidance_scale"] = q["guidance_scale"]
            # ★ 2026-11-13：octree 按**显存**兜底抬升。
            #
            #   问题：笔记本上默认走"快速"（256），出来的模型表面是一块块台阶，
            #   看着就是粗糙。而 256 只占 3.9 GB 显存 —— 8 GB 的卡完全跑得动 384。
            #
            #   规则：显存 ≥ 12 GB 时 256 抬到 384；≥ 24 GB 时抬到 512。
            #   只**抬高**不降低，用户在 config 里写更大值就尊重 config。
            try:
                import torch
                if torch.cuda.is_available():
                    _vram = torch.cuda.get_device_properties(0).total_memory / 1024 ** 3
                    _oc = int(params["octree_resolution"])
                    _floor = 384 if _vram >= 12 else (512 if _vram >= 24 else 0)
                    if _floor and _oc < _floor:
                        self._log(f"🧊 octree {_oc} → {_floor}（显存 {_vram:.0f} GB 跑得动，"
                                  f"256 的表面台阶太明显）")
                        params["octree_resolution"] = _floor
            except Exception:
                pass

            # ★ 直通覆盖（精度对比/终稿控制）：传了就用传的
            if int(octree_override or 0) > 0:
                params["octree_resolution"] = int(octree_override)
            if int(max_facenum or 0) > 0:
                params["max_facenum"] = int(max_facenum)
            self._log(f"🧊 3D 精度档: {quality_name} → octree={params['octree_resolution']} "
                      f"guidance={params['guidance_scale']}"
                      + (f" 面数上限={params['max_facenum']:,}" if params.get("max_facenum") else " 面数=按档自动"))
        
        try:
            workflow_data = self.comfy.build_workflow("3d", params)
            t_start = time.time()   # 本次任务起点：兜底只认此后生成的文件
            prompt_id = self.comfy.submit_workflow(workflow_data, stage="3d")
            self._log(f"3D 任务已提交: {prompt_id[:8]}...")
            
            result = self.comfy.wait_for_completion(prompt_id, timeout=1200, thermal_guard=True)
            outputs = result.get("outputs", {})
            
            # 尝试从输出中提取 3D 模型
            model_path = self.comfy.get_result_3d(outputs)
            if model_path and Path(model_path).exists():
                # 复制到输出目录
                shutil.copy2(model_path, output_path)
                self.state.current_glb_path = output_path
                self.state.steps_done.append("3d")
                self._log(f"✅ 3D 模型已保存: {output_path}")
                return self._finalize_3d(output_path, model_key, quality_name,
                                         input_image, _fallback_tried,
                                         max_facenum=max_facenum, octree_override=octree_override)
            
            # Fallback: Save 3D Mesh 节点直接写到 ComfyUI output 目录
            # （outputs JSON 为空但文件已生成）
            comfy_out = Path(self.config["paths"]["comfyui"]) / "output" / Path(output_path).name
            if comfy_out.exists():
                shutil.copy2(comfy_out, output_path)
                self.state.current_glb_path = output_path
                self.state.steps_done.append("3d")
                self._log(f"✅ 3D 模型已保存: {output_path} (从 ComfyUI output)")
                return self._finalize_3d(output_path, model_key, quality_name,
                                         input_image, _fallback_tried,
                                         max_facenum=max_facenum, octree_override=octree_override)

            # 最后尝试：只认本次任务开始之后生成的 .glb（含 output/3d 子目录）。
            # 原实现用「最近 3 分钟内的任意 glb」，会把上一次甚至无关的旧模型
            # 当成本次结果秒回「建模完成」——已收紧为 t_start 之后。
            out_dir = Path(self.config["paths"]["comfyui"]) / "output"
            glbs = []
            glbs.extend(out_dir.glob("*.glb"))
            glbs.extend((out_dir / "3d").glob("*.glb"))
            fresh = [p for p in glbs if p.stat().st_mtime >= t_start - 5]
            if fresh:
                latest = sorted(fresh, key=lambda p: p.stat().st_mtime, reverse=True)[0]
                shutil.copy2(latest, output_path)
                self.state.current_glb_path = output_path
                self.state.steps_done.append("3d")
                self._log(f"✅ 3D 模型已保存: {output_path} (最新 GLB)")
                return self._finalize_3d(output_path, model_key, quality_name,
                                         input_image, _fallback_tried,
                                         max_facenum=max_facenum, octree_override=octree_override)

            return StageResult(False, error="ComfyUI 未返回 3D 模型文件")
            
        except Exception as e:
            self._log(f"❌ 3D 生成失败: {e}")
            return StageResult(False, error=str(e))
    
    # ─── VRAM 管理 ────────────────────────────────────────
    
    def _free_vram(self):
        """释放 ComfyUI 实例占用的 VRAM（单卡时 image/media 同端口，去重只调一次）"""
        img_port = self.config.get("comfyui_ports", {}).get("image", 8188)
        media_port = self.config.get("comfyui_ports", {}).get("media", img_port)
        ports = sorted({img_port, media_port})
        for port in ports:
            try:
                requests.post(
                    f"http://127.0.0.1:{port}/free",
                    json={"unload_models": True, "free_memory": True},
                    timeout=10
                )
            except Exception as e:
                self._log(f"⚠️ VRAM 释放失败 ({port}): {e}")
        time.sleep(2)
        label = "单实例" if len(ports) == 1 else f"{len(ports)} 实例"
        self._log(f"🧹 VRAM 已释放（{label}: {','.join(str(p) for p in ports)}）")

    def _wait_gpu_cool(self, target_temp: int = 90, max_wait: int = 60):
        """紧急冗余兜底：仅当 GPU 温度超过 90°C 才短暂等待降温（最多 60s）。
        2026-08-18 策略调整（宇凯）：不再 83°C 硬掐任务——渐进降频交给
        thermal_governor.py（>81°C 逐步降频 / ≤77°C 恢复，管理员权限）。
        这里只留 ≥90°C 的极端冗余，正常流程不阻塞。
        """
        import subprocess
        waited = 0
        while waited < max_wait:
            try:
                out = subprocess.check_output(
                    ["nvidia-smi", "--query-gpu=temperature.gpu", "--format=csv,noheader"],
                    timeout=5, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                ).decode().strip()
                temp = int(out.split()[0])
                if temp <= target_temp:
                    if waited >= 10:
                        self._log(f"🧊 GPU 已降温至 {temp}°C（等待 {waited}s）")
                    return
            except Exception:
                pass
            time.sleep(10)
            waited += 10
        if waited >= 10:
            self._log(f"⚠️ GPU 超过 {target_temp}°C 且等待降温超时({max_wait}s)，继续执行")
    
    # ─── STL 导出 ─────────────────────────────────────────
    
    def export_stl(self, glb_path: str, target_size_mm: float = 120.0,
                   optimize_preset: str = None) -> object:
        """GLB → STL 转换 + 优化"""
        from .stages.base import StageResult
        from .mesh_optimizer import MeshOptimizer, OptConfig, preset_fast, preset_standard, preset_high_quality, preset_print_ready
        
        if not glb_path or not Path(glb_path).exists():
            return StageResult(False, error="GLB 文件不存在")
        
        self._log(f"📦 STL 导出: {Path(glb_path).name} → {target_size_mm}mm")
        
        # Select preset
        presets = {
            "fast": preset_fast,
            "standard": preset_standard,
            "high_quality": preset_high_quality,
            "print_ready": preset_print_ready,
        }
        preset_fn = presets.get(optimize_preset, preset_standard)
        config = preset_fn()
        config.target_size_mm = target_size_mm
        
        try:
            output_dir = Path(self.config["paths"]["output"]) / "stl"
            output_dir.mkdir(parents=True, exist_ok=True)
            
            optimizer = MeshOptimizer(output_dir=output_dir)
            result = optimizer.optimize(input_path=glb_path, config=config)
            
            if result.success:
                self.state.steps_done.append("stl")
                self._log(f"✅ STL 已保存: {result.output_path} ({result.output_faces} faces)")
                return StageResult(
                    True, result.output_path,
                    metadata={
                        "vertices": result.output_vertices,
                        "faces": result.output_faces,
                        "reduction_ratio": result.reduction_ratio,
                        "watertight": result.watertight,
                        "min_thickness_mm": result.min_thickness_mm,
                        "overhang_ratio": result.overhang_ratio,
                        "total_time": result.total_time,
                    }
                )
            else:
                return StageResult(False, error=result.error)
                
        except Exception as e:
            self._log(f"❌ STL 导出失败: {e}")
            return StageResult(False, error=str(e))
    
    # ─── 逆向导入 ─────────────────────────────────────────
    
    def import_video(self, video_path: str) -> str:
        """导入视频，提取第一帧"""
        try:
            import cv2
            cap = cv2.VideoCapture(video_path)
            ret, frame = cap.read()
            cap.release()
            
            if ret:
                output_path = str(Path(self.config["paths"]["output"]) / "images" / 
                                f"frame_{int(time.time())}.png")
                cv2.imwrite(output_path, frame)
                self._log(f"✅ 视频帧已提取: {output_path}")
                return output_path
        except ImportError:
            self._log("⚠️ OpenCV 未安装，尝试 ffmpeg...")
        
        # Fallback: ffmpeg
        import subprocess
        output_path = str(Path(self.config["paths"]["output"]) / "images" / 
                        f"frame_{int(time.time())}.png")
        try:
            subprocess.run([
                "ffmpeg", "-i", video_path, "-vframes", "1", 
                "-q:v", "2", output_path
            ], check=True, capture_output=True)
            self._log(f"✅ 视频帧已提取: {output_path}")
            return output_path
        except Exception as e:
            self._log(f"❌ 视频导入失败: {e}")
            return ""
    
    def import_glb(self, glb_path: str):
        """导入 GLB 文件"""
        self.state.current_glb_path = glb_path
        self._log(f"📂 GLB 已导入: {glb_path}")
        return glb_path
    
    # ─── 状态管理 ─────────────────────────────────────────
    
    def reset(self):
        """重置 Pipeline 状态"""
        self.state.reset()
        self._log_lines = []
        self._log("🔄 Pipeline 已重置")
