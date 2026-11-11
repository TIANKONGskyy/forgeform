"""Mesh optimization pipeline for 3D printing.

Modular design — each step is independent and can be toggled.

Tools (by priority):
  - trimesh: basic geometry, scaling, watertight check
  - pymeshfix: hole filling, non-manifold repair, watertight guarantee
  - fast_simplification: quadric edge collapse decimation (QEM)
  - pymeshlab: smoothing, remeshing, advanced filters
  - open3d: point cloud ops, normal estimation (fallback)
  - manifold3d: boolean ops, guaranteed watertight output

Usage:
    from backend.mesh_optimizer import MeshOptimizer, OptConfig
    
    opt = MeshOptimizer()
    result = opt.optimize(glb_path, config=OptConfig(
        target_faces=50000,
        repair=True,
        simplify=True,
        smooth=False,
    ))
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import trimesh


@dataclass
class OptConfig:
    """Optimization configuration."""
    # Target size in mm (longest axis)
    target_size_mm: float = 120.0
    
    # Repair
    repair: bool = True
    remove_degenerate: bool = True
    remove_duplicates: bool = True
    fix_normals: bool = True
    fill_holes: bool = True
    fix_non_manifold: bool = True
    
    # Simplification
    simplify: bool = False
    target_faces: Optional[int] = None  # None = auto (50k-100k based on input)
    simplify_ratio: Optional[float] = None  # 0.0-1.0, overrides target_faces
    aggressive: bool = False  # Allow more aggressive simplification
    
    # Smoothing
    smooth: bool = False
    smooth_iterations: int = 3
    smooth_lambda: float = 0.5
    
    # Remeshing (pymeshlab)
    remesh: bool = False
    remesh_target_edge_length: Optional[float] = None  # mm, None = auto
    
    # Analysis
    analyze_thickness: bool = False
    analyze_overhangs: bool = False
    overhang_angle_deg: float = 45.0
    
    # Export
    export_binary_stl: bool = True
    
    # 底面平整（2026-08-14：加平底座，保证 3D 打印稳定）
    flatten_base: bool = True

    # 漂浮碎片（2026-10-20：独立开关，不再只藏在 repair 里）
    remove_floating: bool = True

    # 固化（2026-09-22）：TripoSplat 产出的是"壳"（百万面、数千碎片、非水密），
    # 切片器无法当实心件。开启后若仍不水密 → 体素化 + Marching Cubes 重建封闭实体。
    solidify: bool = True
    solidify_pitch_mm: Optional[float] = None  # None = 自动（长边/200，限制 0.4~1.5mm）


@dataclass
class OptResult:
    """Optimization result."""
    success: bool
    mesh: Optional[trimesh.Trimesh] = None
    output_path: str = ""
    
    # Stats
    input_vertices: int = 0
    input_faces: int = 0
    output_vertices: int = 0
    output_faces: int = 0
    reduction_ratio: float = 0.0  # 0.0 = no reduction, 1.0 = 100% reduction
    
    # Quality metrics
    watertight: bool = False
    oriented: bool = False  # Consistent face orientation
    
    # Timing
    repair_time: float = 0.0
    simplify_time: float = 0.0
    smooth_time: float = 0.0
    total_time: float = 0.0
    
    # Analysis results
    min_thickness_mm: Optional[float] = None
    overhang_ratio: Optional[float] = None  # 0.0-1.0, ratio needing support
    
    # Log
    log: list[str] = field(default_factory=list)
    error: str = ""


class MeshOptimizer:
    """3D printing mesh optimization pipeline."""
    
    def __init__(self, output_dir: Path = None):
        self.output_dir = Path(output_dir) if output_dir else Path("outputs/stl")
        self.output_dir.mkdir(parents=True, exist_ok=True)
    
    def optimize(self, input_path: str | Path, config: OptConfig = None) -> OptResult:
        """Run full optimization pipeline."""
        if config is None:
            config = OptConfig()
        
        result = OptResult(success=False)
        t0 = time.time()
        
        try:
            # Load
            result.log.append(f"[Load] {input_path}")
            mesh = self._load_mesh(input_path)
            result.input_vertices = len(mesh.vertices)
            result.input_faces = len(mesh.faces)
            result.log.append(f"  → {result.input_vertices} verts, {result.input_faces} faces")
            
            # Scale to target size + slice bottom 2mm（上轴换算已在 _load_mesh 做完，见那里注释）
            mesh = self._scale_to_target(mesh, config.target_size_mm)
            mesh = self._slice_bottom(mesh, 2.0, result.log)
            # 切掉底部 2mm 后底面会停在 z=2mm → 模型在切片软件里悬空 2mm。
            # 这里重新落地，让底面回到 Z=0。
            if abs(mesh.bounds[0][2]) > 1e-6:
                mesh.apply_translation([0, 0, -mesh.bounds[0][2]])
            result.log.append(f"[Slice+Reground] → 底部2mm已切除，底面已落回 Z=0")
            
            # 2026-10-20：漂浮碎片先删，再进 repair/flatten（宇凯要求"建模不要有浮空"）
            if config.remove_floating:
                mesh = self._remove_floating(mesh, result.log)

            # 2026-08-14：底面平整（加平底座，宇凯要求底面平整）
            if config.flatten_base:
                mesh = self._flatten_base(mesh, result.log)
            
            # 2026-08-11：超大网格先简化再修复（pymeshlab 处理百万面会卡死）
            # 仅当 repair 和 simplify 都开启时，先简化（保留目标面数）再修复
            if config.repair and config.simplify and len(mesh.faces) > 200000:
                t1 = time.time()
                mesh = self._simplify(mesh, config, result.log)
                result.simplify_time = time.time() - t1
                result.log.append(f"[Pre-simplify] {result.simplify_time:.2f}s")
                
                # 修复（简化后的网格 pymeshlab 秒修）
                t2 = time.time()
                mesh = self._repair(mesh, config, result.log)
                result.repair_time = time.time() - t2
                result.log.append(f"[Repair] {result.repair_time:.2f}s")
                # 标记已简化，跳过后续重复简化
                already_simplified = True
            else:
                already_simplified = False
                # Repair
                if config.repair:
                    t1 = time.time()
                    mesh = self._repair(mesh, config, result.log)
                    result.repair_time = time.time() - t1
                    result.log.append(f"[Repair] {result.repair_time:.2f}s")
            
            # Solidify 放在「简化之后、导出之前」（2026-09-22）：
            # 简化（边折叠）本身会破坏水密性，所以必须最后固化，
            # 才能保证导出的是封闭实体。TripoSplat 这类"壳"能否打印的关键。
            if config.simplify and not already_simplified:
                t1 = time.time()
                mesh = self._simplify(mesh, config, result.log)
                result.simplify_time = time.time() - t1
                result.log.append(f"[Simplify] {result.simplify_time:.2f}s")

            if config.solidify and not mesh.is_watertight:
                t1 = time.time()
                mesh = self._solidify(mesh, config, result.log)
                result.log.append(f"[Solidify] {time.time() - t1:.2f}s")

            # 末尾重新缩放到目标尺寸（切底/加底座会改变外接尺寸，
            # 原实现只在校准前缩放，实测最终 201mm 远超目标 120mm）
            try:
                ext = mesh.extents
                if len(ext):
                    cur_long = float(max(ext))
                    if cur_long > 0 and abs(cur_long - config.target_size_mm) > 1.0:
                        mesh.apply_scale(config.target_size_mm / cur_long)
                        result.log.append(
                            f"[Rescale] {cur_long:.0f}mm → {config.target_size_mm:.0f}mm")
            except Exception as e:
                result.log.append(f"[Rescale] skipped: {e}")
            
            # Smooth
            if config.smooth:
                t1 = time.time()
                mesh = self._smooth(mesh, config, result.log)
                result.smooth_time = time.time() - t1
                result.log.append(f"[Smooth] {result.smooth_time:.2f}s")
            
            # Remesh
            if config.remesh:
                t1 = time.time()
                mesh = self._remesh(mesh, config, result.log)
                result.log.append(f"[Remesh] {time.time() - t1:.2f}s")
            
            # Final checks
            result.watertight = mesh.is_watertight
            result.oriented = mesh.is_volume  # is_volume implies oriented
            
            if not result.watertight:
                result.log.append("[Warn] Not watertight after optimization")
            
            # Analysis
            if config.analyze_thickness:
                result.min_thickness_mm = self._analyze_thickness(mesh, result.log)
            
            if config.analyze_overhangs:
                result.overhang_ratio = self._analyze_overhangs(
                    mesh, config.overhang_angle_deg, result.log
                )
            
            # 出厂前最后一步：确保底面落在 Z=0。
            # 中途切底 2mm、加平底座（_flatten_base）都可能把模型抬高（实测手机支架那组
            # 停在 Z=2.81mm → 切片软件里悬空）。放在导出前统一收口。
            if abs(mesh.bounds[0][2]) > 1e-6:
                mesh.apply_translation([0, 0, -mesh.bounds[0][2]])
                result.log.append(f"[Reground] 底面校正到 Z=0")

            # Export
            output_path = self._export(mesh, input_path, config)
            result.output_path = str(output_path)
            result.log.append(f"[Export] {output_path}")
            
            # Stats
            result.output_vertices = len(mesh.vertices)
            result.output_faces = len(mesh.faces)
            if result.input_faces > 0:
                result.reduction_ratio = 1.0 - (result.output_faces / result.input_faces)
            
            result.mesh = mesh
            result.success = True
            
        except Exception as e:
            result.error = str(e)
            result.log.append(f"[Error] {e}")
        
        result.total_time = time.time() - t0
        return result
    
    def _load_mesh(self, path: str | Path) -> trimesh.Trimesh:
        """Load GLB/GLTF/OBJ and merge all meshes.

        【2026-09-23 修】GLB/glTF 规范是 **Y 轴朝上**，而打印（Bambu）与我们的预览
        （matplotlib 3D）都是 Z 轴朝上 → 不换算就"躺着"。这里统一转成 Z 朝上，
        且只转一次（STL 没有上轴约定，它本身就是我们产出的 Z-up 资产，不要再转）。
        """
        scene = trimesh.load(str(path), force="scene")
        meshes = [g for g in scene.geometry.values() if isinstance(g, trimesh.Trimesh)]
        if not meshes:
            raise ValueError("No mesh data in file")
        mesh = meshes[0].copy() if len(meshes) == 1 else trimesh.util.concatenate(meshes)
        if str(path).lower().endswith((".glb", ".gltf")):
            mesh = y_up_to_z_up(mesh)
        return mesh
    
    def _scale_to_target(self, mesh: trimesh.Trimesh, target_mm: float) -> trimesh.Trimesh:
        """缩放到目标尺寸、居中、底面落在 Z=0。

        【2026-09-23 修】原来这里还会**无条件绕 X 轴转 90°**（2026-09-20 加的，注释写
        "打印时竖放以获得最佳XY精度"）——对角色类模型就是把熊放倒，而紧随其后的
        _slice_bottom 又按"放倒后"的 Z 切掉底部 2mm（等于切熊的背）✗，用户看到的就是
        "模型躺着"。上轴换算统一在 _load_mesh 里做（Y-up→Z-up），这里不再放倒；
        确需竖放打印的件可用环境变量 LAY_DOWN_FOR_PRINT=1 打开旧行为。
        """
        import math
        lay_down = os.environ.get("LAY_DOWN_FOR_PRINT", "").strip().lower() in ("1", "true", "yes", "on")
        bounds = mesh.bounds
        dims = bounds[1] - bounds[0]
        scale = target_mm / max(dims) if max(dims) > 0 else 1.0
        mesh.apply_scale(scale)
        
        # Center XY, ground Z at 0
        b = mesh.bounds
        cx = (b[0][0] + b[1][0]) / 2
        cy = (b[0][1] + b[1][1]) / 2
        mesh.apply_translation([-cx, -cy, -b[0][2]])
        
        if lay_down:
            angle = math.radians(90)
            cos_a, sin_a = math.cos(angle), math.sin(angle)
            vertices = mesh.vertices.copy()
            # Rotate around X axis: y'=y*cos-z*sin, z'=y*sin+z*cos
            y_new = vertices[:, 1] * cos_a - vertices[:, 2] * sin_a
            z_new = vertices[:, 1] * sin_a + vertices[:, 2] * cos_a
            vertices[:, 1] = y_new
            vertices[:, 2] = z_new
            mesh = trimesh.Trimesh(vertices=vertices, faces=mesh.faces, process=False)

        return mesh
    
    def _slice_bottom(self, mesh: trimesh.Trimesh, mm: float, log: list) -> trimesh.Trimesh:
        """切除模型底部 mm 毫米的部分（保留mm以上的部分）。"""
        try:
            bounds = mesh.bounds
            z_min = bounds[0][2]
            z_cut = z_min + mm
            # 取所有顶点的Z坐标，筛选 >= z_cut 的顶点
            keep_indices = np.where(mesh.vertices[:, 2] >= z_cut)[0]
            if len(keep_indices) < 3:
                log.append(f"  [slice_bottom] 切片后顶点数不足，跳过")
                return mesh
            # 构建顶点映射：旧索引 -> 新索引
            old_to_new = np.full(len(mesh.vertices), -1, dtype=np.int32)
            old_to_new[keep_indices] = np.arange(len(keep_indices), dtype=np.int32)
            # 取新顶点
            new_vertices = mesh.vertices[keep_indices]
            # 筛选面：三个顶点都在 keep_indices 里
            face_mask = (old_to_new[mesh.faces[:, 0]] >= 0) & \
                        (old_to_new[mesh.faces[:, 1]] >= 0) & \
                        (old_to_new[mesh.faces[:, 2]] >= 0)
            new_faces = old_to_new[mesh.faces[face_mask]]
            result = trimesh.Trimesh(vertices=new_vertices, faces=new_faces, process=False)
            result.remove_unreferenced_vertices()
            log.append(f"  [slice_bottom] 已切除底部{mm}mm，剩余{len(result.vertices)}顶点/{len(result.faces)}面")
            return result
        except Exception as e:
            log.append(f"  [slice_bottom] 失败：{e}")
            return mesh
    
    def _remove_floating(self, mesh: trimesh.Trimesh, log: list) -> trimesh.Trimesh:
        """只保留最大连通体，删掉所有漂浮碎片（2026-10-20 独立成步）。"""
        try:
            comps = trimesh.graph.connected_components(mesh.face_adjacency, min_len=1)
            if len(comps) > 1:
                total = len(mesh.faces)
                largest = max(comps, key=len)
                removed = total - len(largest)
                mesh.update_faces(largest)
                mesh.remove_unreferenced_vertices()
                log.append(f"  Removed {len(comps) - 1} floating parts ({removed} faces)，只保留主体")
        except Exception as e:
            log.append(f"  [floating] {e}")
        return mesh

    def _flatten_base(self, mesh: trimesh.Trimesh, log: list) -> trimesh.Trimesh:
        """底面平整（2026-08-14）：加一个平的圆柱底座，保证 3D 打印稳定（宇凯要求底面平整）
        
        用 pymeshlab 布尔 union 合并底座与主体（水密一体）；布尔失败则退回 concatenate。
        """
        try:
            import pymeshlab
            bounds = mesh.bounds
            cx = (bounds[0][0] + bounds[1][0]) / 2
            cy = (bounds[0][1] + bounds[1][1]) / 2
            span = max(bounds[1][0] - bounds[0][0], bounds[1][1] - bounds[0][1])
            radius = span * 0.55  # 底座半径略小于模型 XY 跨度
            # 圆柱底座（底面 Z=0，顶面 Z=3，居中于模型 XY 中心）
            base = trimesh.creation.cylinder(radius=radius, height=3.0, sections=64)
            base.apply_translation([cx, cy, 1.5])
            
            ms = pymeshlab.MeshSet()
            ms.add_mesh(pymeshlab.Mesh(mesh.vertices, mesh.faces))
            ms.add_mesh(pymeshlab.Mesh(base.vertices, base.faces))
            try:
                ms.meshing_csg_union()
                cm = ms.current_mesh()
                mesh = trimesh.Trimesh(vertices=cm.vertex_matrix(), faces=cm.face_matrix(), process=False)
                log.append(f"  底面平整：已加底座（半径{radius:.0f}mm）并布尔合并")
            except Exception as e:
                # 布尔失败，退回 concatenate（后续 pymeshlab 修复会尽量合并）
                mesh = trimesh.util.concatenate([mesh, base])
                log.append(f"  底面平整：加底座（半径{radius:.0f}mm，concatenate 兜底）")
        except Exception as e:
            log.append(f"  [flatten_base] 失败：{e}")
        return mesh
    
    def _repair(self, mesh: trimesh.Trimesh, config: OptConfig, log: list) -> trimesh.Trimesh:
        """Run repair pipeline."""
        # Basic cleanup (trimesh)
        if config.remove_degenerate:
            before = len(mesh.faces)
            # nondegenerate_faces returns a mask
            mask = mesh.nondegenerate_faces()
            mesh.update_faces(mask)
            after = len(mesh.faces)
            if before != after:
                log.append(f"  Removed {before - after} degenerate faces")
        
        if config.remove_duplicates:
            before = len(mesh.vertices)
            mesh.merge_vertices()
            after = len(mesh.vertices)
            if before != after:
                log.append(f"  Merged {before - after} duplicate vertices")
        
        # Remove unreferenced vertices
        mesh.remove_unreferenced_vertices()
        
        # 2026-08-14：无浮空——只保留最大的连通组件（主体），移除所有漂浮碎片
        # （宇凯要求"建模不要有浮空的地方"；原逻辑保留 >0.5% 体积的组件，仍会留漂浮装饰）
        try:
            comps = trimesh.graph.connected_components(mesh.face_adjacency, min_len=1)
            if len(comps) > 1:
                total = len(mesh.faces)
                largest = max(comps, key=len)
                removed = total - len(largest)
                mesh.update_faces(largest)
                mesh.remove_unreferenced_vertices()
                log.append(f"  Removed {len(comps) - 1} floating parts ({removed} faces)，只保留主体")
        except Exception as e:
            log.append(f"  [fragments] {e}")
        
        if config.fix_normals:
            mesh.fix_normals()
            log.append("  Fixed normals")
        
        # Advanced repair (pymeshfix)
        if config.fill_holes or config.fix_non_manifold:
            # 2026-08-11：先用 pymeshlab 保形补洞（pymeshfix 对大网格会退化）
            pymeshlab_ok = False
            try:
                import pymeshlab
                ms = pymeshlab.MeshSet()
                m = pymeshlab.Mesh(mesh.vertices, mesh.faces)
                ms.add_mesh(m)
                ms.meshing_repair_non_manifold_edges()
                ms.meshing_close_holes(maxholesize=200)
                ms.meshing_remove_unreferenced_vertices()
                cm = ms.current_mesh()
                mesh = trimesh.Trimesh(
                    vertices=cm.vertex_matrix(), faces=cm.face_matrix(), process=False)
                mesh.fix_normals()
                pymeshlab_ok = len(mesh.faces) > 100
                log.append(f"  [pymeshlab] holes closed → {len(mesh.faces)} faces")
            except Exception as e:
                log.append(f"  [pymeshlab] Failed: {e}")
            
            # pymeshfix 兜底：不只是 pymeshlab 抛异常时，**补完洞仍不水密**也要上。
            # 实测 2026-10-19：pymeshlab 报 "holes closed"，结果仍有 326 条边界边
            # → watertight=False → 切片器当不了实心件。pymeshfix 能补成真水密
            # （同一网格：56712 面非水密 → 74802 面、watertight=True）。
            if not pymeshlab_ok or not mesh.is_watertight:
                try:
                    import pymeshfix
                    log.append(f"  Running pymeshfix... (pymeshlab watertight={bool(mesh.is_watertight)})")
                    
                    # Convert to pymeshfix format
                    vertices = np.array(mesh.vertices, dtype=np.float64)
                    faces = np.array(mesh.faces, dtype=np.int32)
                    
                    # Clean mesh with pymeshfix
                    meshfix = pymeshfix.MeshFix(vertices, faces)
                    
                    if config.fix_non_manifold:
                        # This handles non-manifold edges and vertices
                        meshfix.repair()
                        log.append("  Fixed non-manifold edges")
                    
                    if config.fill_holes:
                        # repair() also fills small holes (already called above if fix_non_manifold)
                        if not config.fix_non_manifold:
                            meshfix.repair()
                        log.append("  Filled holes")
                    
                    # Get repaired mesh back
                    v_repaired = meshfix.points
                    f_repaired = meshfix.faces
                    
                    # Reconstruct trimesh
                    mesh = trimesh.Trimesh(vertices=v_repaired, faces=f_repaired, process=True)
                    mesh.fix_normals()
                    log.append(f"  [pymeshfix] watertight={bool(mesh.is_watertight)} "
                               f"faces={len(mesh.faces)}")
                    
                except Exception as e:
                    log.append(f"  [pymeshfix] Failed: {e}, continuing with trimesh only")
        
        return mesh
    
    def _simplify(self, mesh: trimesh.Trimesh, config: OptConfig, log: list) -> trimesh.Trimesh:
        """Mesh simplification using quadric edge collapse."""
        input_faces = len(mesh.faces)
        
        # Determine target face count
        if config.simplify_ratio is not None:
            target = max(1000, int(input_faces * config.simplify_ratio))
        elif config.target_faces is not None:
            target = config.target_faces
        else:
            # Auto: aim for 50k-100k faces
            if input_faces > 200000:
                target = 100000
            elif input_faces > 100000:
                target = 50000
            else:
                target = max(10000, input_faces // 2)
        
        if target >= input_faces:
            log.append(f"  Skip simplify: {input_faces} faces ≤ target {target}")
            return mesh
        
        ratio = target / input_faces
        log.append(f"  Target: {input_faces} → {target} faces ({ratio:.1%})")
        
        try:
            import fast_simplification

            # 2026-09-22：fast_simplification 在超大网格上会"提前停止"
            # （实测 2967963 面要降到 120000，agg=4 只降到 544557）。
            # 改为循环调用直到达标或不再收敛。
            agg = 4 if not config.aggressive else 6
            for attempt in range(6):
                cur = len(mesh.faces)
                if cur <= target:
                    break
                ratio_step = 1.0 - (target / cur)
                vertices = np.array(mesh.vertices, dtype=np.float32)
                faces = np.array(mesh.faces, dtype=np.int32)
                try:
                    v_new, f_new = fast_simplification.simplify(
                        vertices, faces, target_reduction=ratio_step, agg=agg,
                    )
                except Exception as e_in:
                    log.append(f"  [fast_simplification] iter{attempt+1} failed: {e_in}")
                    break
                new = trimesh.Trimesh(vertices=v_new, faces=f_new, process=False)
                if len(new.faces) >= cur:      # 不再收敛，别死循环
                    log.append(f"  [simplify] iter{attempt+1} 无进展（{cur}），停止")
                    break
                mesh = new
            mesh.fix_normals()
            log.append(f"  Result: {len(mesh.faces)} faces (target {target})")

        except Exception as e:
            log.append(f"  [fast_simplification] Failed: {e}")
            # Fallback: try pymeshlab
            try:
                mesh = self._simplify_pymeshlab(mesh, target, log)
            except Exception as e2:
                log.append(f"  [pymeshlab] Also failed: {e2}")

        return mesh
    
    def _simplify_pymeshlab(self, mesh: trimesh.Trimesh, target_faces: int, log: list) -> trimesh.Trimesh:
        """Fallback simplification using pymeshlab."""
        import pymeshlab
        
        ms = pymeshlab.MeshSet()
        m = pymeshlab.Mesh(mesh.vertices, mesh.faces)
        ms.add_mesh(m)
        
        # Quadric edge collapse decimation
        ms.meshing_decimation_quadric_edge_collapse(targetfacenum=target_faces)
        
        cm = ms.current_mesh()
        result = trimesh.Trimesh(vertices=cm.vertex_matrix(), faces=cm.face_matrix(), process=False)
        result.fix_normals()
        log.append(f"  [pymeshlab fallback] → {len(result.faces)} faces")
        return result
    
    def _smooth(self, mesh: trimesh.Trimesh, config: OptConfig, log: list) -> trimesh.Trimesh:
        """Laplacian smoothing."""
        try:
            import pymeshlab
            
            ms = pymeshlab.MeshSet()
            m = pymeshlab.Mesh(mesh.vertices, mesh.faces)
            ms.add_mesh(m)
            
            # Use the correct method name for newer pymeshlab
            ms.apply_coord_laplacian_smoothing(
                stepsmoothnum=config.smooth_iterations,
                lambda_=config.smooth_lambda,
            )
            
            cm = ms.current_mesh()
            result = trimesh.Trimesh(vertices=cm.vertex_matrix(), faces=cm.face_matrix(), process=False)
            result.fix_normals()
            log.append(f"  Smoothed {config.smooth_iterations} iterations")
            return result
            
        except Exception as e:
            # Fallback: trimesh smoothing
            try:
                trimesh.smoothing.filter_laplacian(
                    mesh, iterations=config.smooth_iterations, lamb=config.smooth_lambda
                )
                log.append(f"  [trimesh fallback] Smoothed {config.smooth_iterations} iterations")
            except Exception as e2:
                log.append(f"  [smooth] Both methods failed: {e}, {e2}")
            return mesh
    
    def _remesh(self, mesh: trimesh.Trimesh, config: OptConfig, log: list) -> trimesh.Trimesh:
        """Isotropic remeshing for uniform edge lengths."""
        try:
            import pymeshlab
            
            ms = pymeshlab.MeshSet()
            m = pymeshlab.Mesh(mesh.vertices, mesh.faces)
            ms.add_mesh(m)
            
            if config.remesh_target_edge_length:
                target_len = config.remesh_target_edge_length
            else:
                # Auto: based on model size
                bounds = mesh.bounds
                dims = bounds[1] - bounds[0]
                target_len = max(dims) / 100.0  # ~100 edges across longest axis
            
            ms.meshing_isotropic_explicit_remeshing(targetlen=pymeshlab.PureValue(target_len))
            
            cm = ms.current_mesh()
            result = trimesh.Trimesh(vertices=cm.vertex_matrix(), faces=cm.face_matrix(), process=False)
            result.fix_normals()
            log.append(f"  Remeshed (target edge {target_len:.2f}mm)")
            return result
            
        except Exception as e:
            log.append(f"  [remesh] Failed: {e}")
            return mesh
    
    def _solidify(self, mesh: trimesh.Trimesh, config: OptConfig, log: list) -> trimesh.Trimesh:
        """把开放的"壳"重建成封闭实心网格（可打印）。

        2026-09-22：单图重建（TripoSplat 等）输出的是有缝薄壳 + 数千碎片，
        watertight=False，切片器无法当实心件。做法：
            体素化 → 形态学闭运算(封缝) → 填内部 → Marching Cubes
        闭运算是关键：壳有缝时直接 fill() 会漏空，得不到实心（实测 0.59mm
        体素直接 fill+marching 出来 watertight 仍为 False）。不水密则放粗重试。
        """
        import math
        import numpy as np
        import scipy.ndimage as ndi
        from trimesh.voxel import ops as vops

        ext = mesh.extents
        longest = float(max(ext)) if len(ext) else 120.0

        if config.solidify_pitch_mm:
            pitch = float(config.solidify_pitch_mm)
        elif config.target_faces:
            # 经验关系 面数 ≈ k/pitch²（实测 0.59mm → 1,076,016 面）
            k = 1076016 * (0.59 ** 2)
            pitch = math.sqrt(k / max(int(config.target_faces), 1000))
            pitch = min(3.0, max(0.35, pitch))
        else:
            pitch = min(1.5, max(0.4, longest / 200.0))

        best = mesh
        for attempt in range(4):
            try:
                vg = mesh.voxelized(pitch=pitch)
                occ = np.asarray(vg.matrix, dtype=bool)
                if occ.size == 0 or not occ.any():
                    raise ValueError("empty voxel grid")
                occ = ndi.binary_closing(occ, structure=np.ones((3, 3, 3)), iterations=2)
                occ = ndi.binary_fill_holes(occ)
                solid = vops.matrix_to_marching_cubes(occ, pitch=pitch)
                solid.fix_normals()
                solid.remove_unreferenced_vertices()
                wt = bool(solid.is_watertight)
                log.append(f"  Solidify#{attempt+1}: voxel {pitch:.2f}mm → "
                           f"watertight={wt} faces={len(solid.faces)}")
                best = solid
                if wt:
                    return solid
                pitch *= 1.6          # 放粗一格再试
            except Exception as e:
                log.append(f"  [solidify] #{attempt+1} failed: {e}")
                pitch *= 1.6
        return best

    def _analyze_thickness(self, mesh: trimesh.Trimesh, log: list) -> Optional[float]:
        """Estimate minimum wall thickness using ray casting."""
        try:
            # Check if rtree is available (needed for ray casting)
            try:
                import rtree  # noqa: F401
            except ImportError:
                log.append("  [thickness] Skipped: rtree module not installed")
                return None
            
            # Sample points and cast rays inward
            # This is a simplified approximation
            samples = min(1000, len(mesh.vertices))
            indices = np.random.choice(len(mesh.vertices), samples, replace=False)
            
            min_thickness = float('inf')
            for idx in indices:
                v = mesh.vertices[idx]
                n = mesh.vertex_normals[idx]
                
                # Cast ray inward (opposite to normal)
                ray_origin = v - n * 0.01
                ray_dir = -n
                
                locs, _, _ = mesh.ray.intersects_location(
                    ray_origins=[ray_origin],
                    ray_directions=[ray_dir],
                )
                
                if len(locs) > 0:
                    dists = np.linalg.norm(locs - ray_origin, axis=1)
                    if len(dists) > 0:
                        thickness = np.min(dists)
                        min_thickness = min(min_thickness, thickness)
            
            if min_thickness == float('inf'):
                min_thickness = 0.0
            
            log.append(f"  Min thickness: {min_thickness:.2f}mm")
            return min_thickness
            
        except Exception as e:
            log.append(f"  [thickness] Failed: {e}")
            return None
    
    def _analyze_overhangs(self, mesh: trimesh.Trimesh, angle_deg: float, log: list) -> Optional[float]:
        """Analyze overhang areas that need support."""
        try:
            # Z-axis is up for printing
            z_axis = np.array([0, 0, 1])
            
            # Face normals
            normals = mesh.face_normals
            # Angle between face normal and Z-axis
            cos_angles = np.dot(normals, z_axis)
            angles = np.arccos(np.clip(cos_angles, -1, 1)) * 180 / np.pi
            
            # Overhang: faces pointing downward beyond threshold
            overhang_mask = angles > (180 - angle_deg)
            overhang_area = mesh.area_faces[overhang_mask].sum()
            total_area = mesh.area_faces.sum()
            
            ratio = overhang_area / total_area if total_area > 0 else 0.0
            log.append(f"  Overhang ratio: {ratio:.1%} (>{angle_deg}°)")
            return ratio
            
        except Exception as e:
            log.append(f"  [overhangs] Failed: {e}")
            return None
    
    def _export(self, mesh: trimesh.Trimesh, input_path: str | Path, config: OptConfig) -> Path:
        """Export optimized STL."""
        stem = Path(input_path).stem
        output_path = self.output_dir / f"{stem}_optimized.stl"
        
        # Use binary STL (smaller, faster)
        mesh.export(str(output_path), file_type="stl")
        return output_path


# Convenience presets
def preset_fast() -> OptConfig:
    """Quick optimization — basic repair only."""
    return OptConfig(
        repair=True,
        simplify=False,
        smooth=False,
        remesh=False,
        analyze_thickness=False,
        analyze_overhangs=False,
    )


def preset_standard() -> OptConfig:
    """Standard — repair + moderate simplification."""
    return OptConfig(
        repair=True,
        simplify=True,
        target_faces=80000,
        smooth=False,
        remesh=False,
        analyze_thickness=True,
        analyze_overhangs=True,
    )


def preset_high_quality() -> OptConfig:
    """High quality — full optimization pipeline, 细节优先（2026-08-14：50 万面保浮雕细节）"""
    return OptConfig(
        repair=True,
        simplify=True,
        target_faces=500000,   # 2026-08-14：调研结论 120mm 浮雕保真需 0.5-1M 面
        smooth=True,
        smooth_iterations=2,
        remesh=False,
        analyze_thickness=True,
        analyze_overhangs=True,
    )


def preset_print_ready() -> OptConfig:
    """Print-ready — guaranteed watertight + analysis.
    2026-08-11：超大网格（TripoSplat 100万+面）自动简化到可修复规模，
    细节保留最大化（目标 30 万面，远超 3D 打印需要）
    """
    return OptConfig(
        repair=True,
        fill_holes=True,
        fix_non_manifold=True,
        simplify=True,          # 2026-08-11：开启，超大网格必须简化才能水密
        target_faces=200000,    # 20 万面：保住细节；再简化会让 pymeshfix/solidify 掉细节
        smooth=True,
        smooth_iterations=3,
        remesh=False,
        analyze_thickness=True,
        analyze_overhangs=True,
    )

# ─── 上轴换算（模块级：GLB/glTF 是 Y-up，打印与预览都是 Z-up）───────────────

def y_up_to_z_up(mesh: "trimesh.Trimesh") -> "trimesh.Trimesh":
    """绕 X 转 +90°：(x, y, z) → (x, -z, y)，把原本的 +Y（glTF 的上）变成 +Z（我们的上）。

    只对 GLB/GLTF 调用。STL 没有上轴约定，而且它是我们产出的 Z-up 资产，不能重复转。
    """
    v = np.asarray(mesh.vertices, dtype=float).copy()
    out = np.empty_like(v)
    out[:, 0] = v[:, 0]
    out[:, 1] = -v[:, 2]
    out[:, 2] = v[:, 1]
    return trimesh.Trimesh(vertices=out, faces=np.asarray(mesh.faces).copy(), process=False)
