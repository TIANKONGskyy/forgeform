import numpy as np
import trimesh

from backend.mesh_optimizer import MeshOptimizer, preset_standard


def test_standard_export_preserves_box_body(tmp_path):
    source = tmp_path / "box.glb"
    trimesh.creation.box(extents=[1, 2, 3]).export(source)
    config = preset_standard()
    config.target_size_mm = 80

    result = MeshOptimizer(output_dir=tmp_path).optimize(str(source), config)

    assert result.success, result.error
    mesh = trimesh.load(result.output_path, force="mesh")
    assert mesh.is_watertight
    assert mesh.body_count == 1
    assert mesh.extents[2] > 20
    assert np.isclose(max(mesh.extents), 80, atol=0.01)
    assert np.isclose(mesh.bounds[0, 2], 0, atol=0.01)
