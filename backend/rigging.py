"""自动绑定的产品入口（2026-11-26 整合）。

## 来源
并自 Codex 的 `backend/rigging.py`，但**换掉了后端**：
Codex 那版只认 `runtime/UniRig` 那套本地运行环境；实测 UniRig 在 V100 上跑不起来
（`PTv3Object.py` 硬 `import flash_attn` + FA2 无 sm70 内核，要改 3 处约 0.5–1 天）。

本仓库现在有两条**验证过**的绑定路径（见 `work/research/rig/RESULT.md`）：

| 路径 | 适用 | 耗时 | 显存 | 质量 |
|---|---|---|---|---|
| 模板骨架 + 热扩散权重 | 硬表面 / 任意网格 | 8–68 s | 0（Cycles CPU） | 0 个无权重顶点 |
| RigAnything | 角色（52 根语义骨含手指） | 52 s | 4.7 GB | 撕裂 0.061% |

两者的执行器都在 `tools/asset_refine_rig.py`（`rig` 子命令），本模块只负责
**调度与守门**：占位锁、GPU 争用检查、产物校验（GLB 里必须真有 skin）。

## 保留自 Codex 的部分
- 拒绝抢占：有生成/对话任务时直接报错，不偷偷卸载别人的服务
- 只产出新文件，绝不覆盖原始资产
- **用二进制校验 GLB 里真有 skins/joints**，不信子进程退出码
"""
from __future__ import annotations

import json
import os
import shutil
import struct
import subprocess
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "tools" / "asset_refine_rig.py"
PY = os.environ.get("FORGEFORM_PYTHON", r"D:\miniconda3\python.exe")
LOCK = ROOT / "runtime" / "rigging.lock"


def _gpu_info():
    """(空闲显存 MiB, 温度 C)；拿不到就返回 (None, None) 不阻塞。"""
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=memory.free,temperature.gpu",
             "--format=csv,noheader,nounits", "-i", "0"],
            timeout=10, text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        free, temp = out.splitlines()[0].split(",")[:2]
        return int(free.strip()), int(temp.strip())
    except Exception:
        return None, None


def _busy_reason():
    """有别的重活儿在跑就拒绝，不硬上。"""
    try:
        from . import gpu_state
        if gpu_state.comfy_busy():
            return "ComfyUI 正在出图/建模"
        if hasattr(gpu_state, "is_chatting") and gpu_state.is_chatting():
            return "对话模型正在生成"
    except Exception:
        pass
    return ""


def _validate_glb(path: Path) -> dict:
    """不信退出码：解 GLB 二进制，确认有 skins + JOINTS_0/WEIGHTS_0。"""
    with path.open("rb") as f:
        if f.read(4) != b"glTF":
            raise RuntimeError("绑定结果不是有效 GLB")
        f.read(8)
        length, chunk_type = struct.unpack("<II", f.read(8))
        if chunk_type != 0x4E4F534A or length > 128 * 1024 * 1024:
            raise RuntimeError("绑定结果缺少有效 GLB 元数据")
        data = json.loads(f.read(length))
    skins = data.get("skins") or []
    joint_count = sum(len(s.get("joints") or []) for s in skins)
    has_weights = any(
        "JOINTS_0" in (p.get("attributes") or {}) and "WEIGHTS_0" in (p.get("attributes") or {})
        for mesh in (data.get("meshes") or [])
        for p in (mesh.get("primitives") or []))
    if not skins or not joint_count or not has_weights:
        raise RuntimeError(f"输出缺少骨架或蒙皮（skins={len(skins)} joints={joint_count} "
                           f"weights={has_weights}）")
    return {"skins": len(skins), "joints": joint_count, "bytes": path.stat().st_size}


def bind_model(input_path, output_dir, *, tier="template", template="auto",
               timeout=900) -> dict:
    """把一个 GLB/STL 绑上骨架，产出**新文件**（原资产不动）。

    Args:
        tier: "template"（模板骨架，CPU，8–68 s）| "riganything"（51 骨角色档，需 4.7 GB 显存）
        template: auto|humanoid|quadruped|aircraft|chain
    Returns: {"path","log","job","joints","seconds","tier"}
    """
    src = Path(input_path).resolve()
    if not src.is_file() or src.suffix.lower() not in (".glb", ".stl", ".obj"):
        raise ValueError("请提供 .glb / .stl / .obj 模型")
    if not TOOL.is_file():
        raise RuntimeError(f"缺少绑定执行器 {TOOL}")

    reason = _busy_reason()
    if reason:
        raise RuntimeError(f"当前有任务在跑（{reason}），请等它完成再绑定")
    if tier == "riganything":
        free, temp = _gpu_info()
        if free is not None and free < 4500:
            raise RuntimeError(f"空闲显存只有 {free} MiB，RigAnything 需要约 4.7 GB")
        if temp is not None and temp >= 84:
            raise RuntimeError(f"GPU 温度 {temp}°C 偏高，先降负载")
    else:
        _, temp = _gpu_info()
        if temp is not None and temp >= 88:
            raise RuntimeError(f"GPU 温度 {temp}°C，先散热")

    LOCK.parent.mkdir(parents=True, exist_ok=True)
    job = uuid.uuid4().hex
    try:
        fd = os.open(str(LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise RuntimeError("已有绑定任务在跑，请等它结束")
    out_dir = Path(output_dir)
    t0 = time.monotonic()
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"pid": os.getpid(), "job": job}, f)
        env = dict(os.environ)
        env.update(PYTHONPATH="", PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
        proc = subprocess.run(
            [PY, str(TOOL), "rig", "--mesh", str(src), "--out-dir", str(out_dir),
             "--tier", tier, "--template", template],
            capture_output=True, env=env, cwd=str(ROOT), timeout=timeout)
        log = (proc.stdout or b"").decode("utf-8", "replace") + \
              (proc.stderr or b"").decode("utf-8", "replace")
        produced = sorted(
            [p for p in list(out_dir.glob("*_rigged.glb")) + list(out_dir.rglob("*.glb"))
             if p.name.endswith("_rig.glb") or p.name.endswith("_rigged.glb")],
            key=lambda p: p.stat().st_mtime, reverse=True)
        if not produced:
            raise RuntimeError(f"绑定没有产出 GLB（退出码 {proc.returncode}）：{log[-400:]}")
        info = _validate_glb(produced[0])
        target = out_dir / f"rigged_{job[:12]}.glb"
        shutil.copy2(produced[0], target)
        return {"path": str(target), "log": log[-2000:], "job": job,
                "joints": info["joints"], "skins": info["skins"],
                "seconds": round(time.monotonic() - t0, 1), "tier": tier}
    finally:
        try:
            if json.loads(LOCK.read_text(encoding="utf-8")).get("job") == job:
                LOCK.unlink()
        except (OSError, ValueError):
            pass
