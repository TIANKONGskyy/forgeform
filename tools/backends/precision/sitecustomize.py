"""Auto-injection shim: makes `import flash_attn` resolve to the pure-PyTorch compat layer.

Put this directory on PYTHONPATH and any Python process (infer.py, app.py, your own script)
imports it automatically at startup:

    $env:PYTHONPATH = 'D:\\ForgeForm-oss\\work\\research\\precision'
    D:\\miniconda3\\python.exe infer.py --model <weights> --resolution 512 ...

It only acts when the real flash-attn is missing, so a machine with a working flash-attn 2
still uses it.  Two extra conveniences, both required on this box:

* ``os.add_dll_directory`` for the CUDA / torch DLL folders -- Python 3.8+ no longer resolves
  extension-module dependencies through PATH, which is why ``cudavoxelize`` and
  ``fillhole_cpp`` fail with "DLL load failed" when only PATH is set.
* ``third_party/{voxelization,fill_holes}`` on sys.path when the repo is next to this file.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = Path(r"D:\AI-Workshop\work\feiji_precision_vh\Direct3D-V3")

if os.environ.get("FLASH_ATTN_COMPAT_DISABLE", "") not in ("1", "true", "True"):
    try:  # real flash-attn present? leave it alone
        import flash_attn  # noqa: F401
    except Exception:
        if _HERE.is_dir():
            sys.path.insert(0, str(_HERE))
        try:
            import flash_attn_compat as _compat

            sys.modules.setdefault("flash_attn", _compat)
        except Exception as exc:  # pragma: no cover
            print(f"[flash_attn_compat] injection failed: {exc}", file=sys.stderr)

for _d in (
    r"C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.8\bin",
    r"D:\miniconda3\Lib\site-packages\torch\lib",
    r"D:\miniconda3\Library\bin",
):
    if os.path.isdir(_d):
        try:
            os.add_dll_directory(_d)
        except OSError:
            pass

for _p in (_REPO / "third_party" / "voxelization", _REPO / "third_party" / "fill_holes"):
    if _p.is_dir() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
if _REPO.is_dir() and str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))
