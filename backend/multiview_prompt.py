"""Four-view reconstruction with optional text-guided reference editing.

## 来源与定位
2026-11-26 从 Codex 那边并进开源铸想。它补上了 `Pipeline.generate_3d_multiview()`
没有的两块：

1. **文字引导的参考图编辑**（`generate_reference`）：先改前视图，人眼确认后再重建
2. **透明通道重接**（`JoinImageWithAlpha`）：LoadImage 会把 alpha 拆成反向 mask，
   已经抠好的图如果不再接回 RGBA，`Multi Background Remover` 会**再抠一遍**，
   机翼/桨叶这类薄结构直接丢

## 与产品入口的关系
- 底层重建能力用本仓库的 `Pipeline.generate_3d_multiview()`（白底原图直传，
  实测 84 s / 490k 面 / 水密；且已处理「不能先 /free」「必须 2mv 模式」两个坑）
- 本模块负责**上层的视图编排**：顺序校验、单视图/多视图分流、参考图编辑、几何闸门
"""
from pathlib import Path
import time
import uuid
import requests
from .pipeline import Pipeline

VIEWS = ("front", "back", "left", "right")


def build_real_workflow(views, model, output_name, seed=12345, octree=384, alpha_views=None):
    available = _ordered_views(views, check_files=False)
    if len(available) < 2:
        raise ValueError("多视图重建至少需要正面和背面")
    wf = {str(i + 1): {"class_type": "LoadImage", "inputs": {"image": views[k]}}
          for i, k in enumerate(available)}
    wf["5"] = {"class_type": "[Comfy3D] Multi Background Remover", "inputs": {
        "image_" + k: [str(i + 1), 0] for i, k in enumerate(available)}}
    wf["6"] = {"class_type": "[Comfy3D] Load Hunyuan3D V2 ShapeGen Pipeline", "inputs": {
        # ★ 2026-11-26 坑：config 里 3D 模型可能写的是单视图 generation_mode
        #   （如 Hunyuan3D-2-Fast）。原样传下去 MV 节点会加载单视图管线，
        #   四视图 dict 进去后预处理器对 None 调 cv2.resize → 60 秒内必崩。
        "generation_mode": _mv_mode(model),
        "weights_format": "safetensors", "flash_vdm": True}}
    wf["7"] = {"class_type": "[Comfy3D] Hunyuan3D V2 ShapeGen MV", "inputs": {
        "shapegen_pipe": ["6", 0], "images": ["5", 0], "seed": seed,
        "guidance_scale": 5.0, "num_inference_steps": 30, "octree_resolution": octree}}
    wf["8"] = {"class_type": "[Comfy3D] Save 3D Mesh", "inputs": {
        "mesh": ["7", 0], "save_path": output_name}}
    # LoadImage exposes transparency as an inverted mask and RGB separately.
    # Rejoin them before the multi-view preprocessor or it will segment an
    # already cut-out image again, losing thin geometry such as propellers.
    for i, key in enumerate(available):
        if key in (alpha_views or ()):
            nid = str(9 + i)
            wf[nid] = {"class_type": "JoinImageWithAlpha", "inputs": {
                "image": [str(i + 1), 0], "alpha": [str(i + 1), 1]}}
            wf["5"]["inputs"]["image_" + key] = [nid, 0]
    return {"prompt": wf, "negative_prompt": ""}


def _mv_mode(model):
    """把任何配置纠正成 2mv 系列（单视图模式跑不了四视图）。"""
    gm = str((model or {}).get("generation_mode") or "")
    return gm if "mv" in gm.lower() else "Hunyuan3D-2mv-Fast"


def _ordered_views(views, check_files=True):
    available = [key for key in VIEWS if views.get(key)]
    if not available:
        raise ValueError("请至少上传一张参考图")
    for key in available:
        if check_files and not Path(views[key]).is_file():
            raise ValueError(f"{key} 视图文件不存在")
    # The installed 3D-Pack node loses the labels and assigns a list in this
    # fixed order. Reject holes rather than silently swap the directions.
    if len(available) > 1 and available != list(VIEWS[:len(available)]):
        raise ValueError("现有多视图节点需要按正、背、左、右顺序提供视图；请补齐缺少的前序视角")
    return available


def _has_transparency(path):
    from PIL import Image
    with Image.open(path) as image:
        if "A" not in image.getbands() and "transparency" not in image.info:
            return False
        low, high = image.convert("RGBA").getchannel("A").getextrema()
        return low < 255 and high > 0


def generate_reference(views, prompt, seed=12345):
    """Generate a reviewable text-guided reference; it may change identity."""
    available = _ordered_views(views)
    if not (prompt or "").strip():
        raise ValueError("请先填写文字要求")
    pipe = Pipeline()
    client = pipe.comfy
    outdir = Path(pipe.config["paths"]["output"]) / "multiview_prompt" / uuid.uuid4().hex
    outdir.mkdir(parents=True, exist_ok=False)
    # Feeding four angles to this image editor often creates a four-panel
    # collage. Edit the front alone; reconstruction still consumes all views.
    image_ref = client.upload_image(str(views[available[0]]), stage="image")
    model = pipe.config["models"]["image"]["qwen21_ref"]
    text = ("Edit the reference image. Keep the exact same character or object, face, "
            "shape, proportions, pose and clothing. Produce one single full-body front view. "
            "No other changes. Requested changes: " + prompt.strip())
    wf = client.build_workflow("image", {"model_config": model, "reference_image": image_ref,
        "prompt": text, "resolution_px": 512,
        "steps": model.get("steps", 20), "seed": seed})
    pid = client.submit_workflow(wf, stage="image")
    result = client.wait_for_completion(pid, timeout=1200, thermal_guard=False)
    url = client.get_result_image(result.get("outputs", {}), stage="image")
    if not url:
        raise RuntimeError("文字参考图生成失败：" + str(result.get("status", result)))
    response = requests.get(url, timeout=60)
    response.raise_for_status()
    preview = outdir / "text_guided_front.png"
    preview.write_bytes(response.content)
    return str(preview)


def generate(views, quality="标准", seed=12345, edited_front=None):
    """Reconstruct the four supplied views, optionally with a reviewed edited front."""
    available = _ordered_views(views)
    if edited_front and not Path(edited_front).is_file():
        raise ValueError("文字参考图不存在，请重新生成")
    pipe = Pipeline()
    client = pipe.comfy
    token = uuid.uuid4().hex
    outdir = Path(pipe.config["paths"]["output"]) / "multiview_prompt" / token
    outdir.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    refs = {k: client.upload_image(str(edited_front if k == available[0] and edited_front else views[k]), stage="3d") for k in available}
    octree = {"快速": 256, "标准": 384, "高质量": 512}.get(quality, 384)
    filename = "mv_" + token + ".glb"
    if len(available) == 1:
        source_image = edited_front or views[available[0]]
        has_alpha = _has_transparency(source_image)
        wf = client.build_workflow("3d", {"model_config": pipe.config["models"]["3d"]["hunyuan21"],
            "input_image": refs[available[0]], "input_has_alpha": has_alpha, "output_path": filename,
            "seed": seed, "texgen": False, "octree_resolution": octree,
            "guidance_scale": 6.0, "steps": 30,
            "max_facenum": 800000 if quality == "高质量" else 0})
    else:
        alpha_views = {k for k in available if _has_transparency(
            edited_front if k == available[0] and edited_front else views[k])}
        wf = build_real_workflow(refs, pipe.config["models"]["3d"]["multiview"], filename, seed, octree,
                                 alpha_views=alpha_views)
    pid = client.submit_workflow(wf, stage="3d")
    result = client.wait_for_completion(pid, timeout=1800, thermal_guard=False)
    source = Path(pipe.config["paths"]["comfyui"]) / "output" / filename
    if not source.is_file():
        raise RuntimeError("建模失败：" + str(result.get("error") or result.get("status", result)))
    target = outdir / "model.glb"
    import shutil
    shutil.copy2(source, target)
    parts, faces = pipe._mesh_parts(str(target))
    flat, _ = pipe._mesh_is_flat_slab(str(target))
    if parts < 0 or parts > 200 or faces < 100 or flat:
        raise RuntimeError(f"模型未通过几何检查：{parts} 个连通块、{faces} 面" + ("，疑似背景板" if flat else ""))
    return str(target), time.monotonic() - started
