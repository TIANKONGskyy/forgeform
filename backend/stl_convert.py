"""GLB → STL conversion for 3D printing."""
from pathlib import Path

import numpy as np
import trimesh


def glb_to_stl(
    glb_path: str,
    target_size_mm: float = 120.0,
    output_dir: str | None = None,
) -> Path:
    """Convert GLB to print-ready STL.

    - Merges all meshes
    - Scales so longest edge = target_size_mm
    - Centers XY, grounds Z at 0
    - Exports binary STL
    """
    glb = trimesh.load(glb_path, force="scene")
    meshes = [g for g in glb.geometry.values() if isinstance(g, trimesh.Trimesh)]
    if not meshes:
        raise RuntimeError("No mesh found in GLB")

    combined = trimesh.util.concatenate(meshes)

    # Scale to target size
    bounds = combined.bounds
    dims = bounds[1] - bounds[0]
    scale = target_size_mm / max(dims)
    combined.apply_scale(scale)

    # Center XY, ground Z at 0
    b = combined.bounds
    cx = (b[0][0] + b[1][0]) / 2
    cy = (b[0][1] + b[1][1]) / 2
    combined.apply_translation([-cx, -cy, -b[0][2]])

    # Determine output path
    if output_dir:
        out_dir = Path(output_dir)
    else:
        out_dir = Path(glb_path).parent
    out_dir.mkdir(parents=True, exist_ok=True)
    stl_path = out_dir / f"{Path(glb_path).stem}_print.stl"
    combined.export(str(stl_path), file_type="stl")

    faces = len(combined.faces)
    verts = len(combined.vertices)
    wt = combined.is_watertight
    print(f"[STL] {stl_path} | {verts} verts, {faces} faces, watertight={wt}")
    return stl_path
