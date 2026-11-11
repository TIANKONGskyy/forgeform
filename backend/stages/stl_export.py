"""STL export stage — GLB → optimized STL for 3D printing."""
from pathlib import Path

from backend.stages.base import Stage, StageResult


class STLExportStage(Stage):

    @property
    def name(self) -> str:
        return "stl_export"

    @property
    def display_name(self) -> str:
        return "STL 导出"

    @property
    def icon(self) -> str:
        return "🖨"

    def build_workflow(self, model_key: str, model_cfg: dict,
                       quality: dict, **kwargs) -> dict:
        # STL export doesn't use ComfyUI — it's a local operation
        return {}

    def process_output(self, outputs: dict, model_key: str,
                       model_cfg: dict, **kwargs) -> StageResult:
        # Not used — STL export runs locally
        return StageResult(True)

    def execute(self, model_key: str = None, quality: dict = None,
                progress_cb=None, **kwargs) -> StageResult:
        """Override: STL export with optional optimization."""
        import time
        from backend.mesh_optimizer import (
            MeshOptimizer, OptConfig,
            preset_fast, preset_standard, preset_high_quality, preset_print_ready,
        )

        glb_path = kwargs.get("glb_path", "")
        target_size = float(kwargs.get("target_size_mm",
                                        self.stage_config.get("default_size_mm", 120)))
        
        # Optimization preset: "fast", "standard", "high_quality", "print_ready", or None (legacy)
        optimize_preset = kwargs.get("optimize_preset", None)
        
        # Individual optimization flags (override preset if provided)
        optimize_enabled = kwargs.get("optimize_enabled", False)
        target_faces = kwargs.get("target_faces", None)
        smooth = kwargs.get("smooth", False)
        analyze = kwargs.get("analyze", False)

        if not glb_path or not Path(glb_path).exists():
            return StageResult(False, error=f"GLB 文件不存在: {glb_path}")

        t0 = time.time()
        
        # Determine config
        if optimize_preset == "fast":
            config = preset_fast()
        elif optimize_preset == "standard":
            config = preset_standard()
        elif optimize_preset == "high_quality":
            config = preset_high_quality()
        elif optimize_preset == "print_ready":
            config = preset_print_ready()
        elif optimize_enabled:
            # Custom config from individual flags
            config = OptConfig(
                repair=True,
                simplify=target_faces is not None and target_faces > 0,
                target_faces=target_faces,
                smooth=smooth,
                analyze_thickness=analyze,
                analyze_overhangs=analyze,
            )
        else:
            # Legacy mode: basic conversion only
            config = OptConfig(
                repair=False,
                simplify=False,
                smooth=False,
                remesh=False,
                analyze_thickness=False,
                analyze_overhangs=False,
            )
        
        config.target_size_mm = target_size

        self.log.log(self.name, "start", "running",
                     params={
                         "source": glb_path,
                         "target_mm": target_size,
                         "preset": optimize_preset or ("custom" if optimize_enabled else "legacy"),
                     })

        if progress_cb:
            preset_name = optimize_preset or ("自定义" if optimize_enabled else "基础转换")
            progress_cb(f"🖨 导出 STL... ({preset_name}, 目标 {target_size}mm)")

        try:
            optimizer = MeshOptimizer(output_dir=self.client.output_root / "stl")
            result = optimizer.optimize(glb_path, config)
            
            dur = time.time() - t0
            
            if not result.success:
                self.log.log(self.name, "complete", "failed",
                             duration_s=dur, error=result.error)
                return StageResult(False, error=result.error, duration_s=dur)

            # Build metadata
            meta = {
                "vertices": result.output_vertices,
                "faces": result.output_faces,
                "input_vertices": result.input_vertices,
                "input_faces": result.input_faces,
                "reduction_ratio": result.reduction_ratio,
                "watertight": result.watertight,
                "oriented": result.oriented,
                "size_mm": target_size,
                "preset": optimize_preset or ("custom" if optimize_enabled else "legacy"),
                "repair_time": result.repair_time,
                "simplify_time": result.simplify_time,
                "smooth_time": result.smooth_time,
            }
            
            if result.min_thickness_mm is not None:
                meta["min_thickness_mm"] = result.min_thickness_mm
            if result.overhang_ratio is not None:
                meta["overhang_ratio"] = result.overhang_ratio
            
            # Log summary
            summary_parts = [f"{result.output_faces} faces"]
            if result.reduction_ratio > 0:
                summary_parts.append(f"-{result.reduction_ratio:.0%}")
            if result.watertight:
                summary_parts.append("watertight")
            else:
                summary_parts.append("NOT watertight")
            if result.min_thickness_mm is not None:
                summary_parts.append(f"min {result.min_thickness_mm:.2f}mm")
            if result.overhang_ratio is not None:
                summary_parts.append(f"overhang {result.overhang_ratio:.0%}")
            
            self.log.log(self.name, "complete", "completed",
                         duration_s=dur, output=result.output_path, params=meta)

            return StageResult(
                True,
                output_path=result.output_path,
                metadata=meta,
                duration_s=dur,
            )

        except Exception as e:
            dur = time.time() - t0
            self.log.log(self.name, "complete", "failed",
                         duration_s=dur, error=str(e))
            return StageResult(False, error=str(e), duration_s=dur)
