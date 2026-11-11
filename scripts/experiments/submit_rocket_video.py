# -*- coding: utf-8 -*-
"""H3 火箭视频:首帧=火箭图,尾帧=水平镜像(180°旋转动势),turbo v4 8步"""
import urllib.request, json, time, os, uuid, sys
sys.stdout.reconfigure(encoding='utf-8')
from PIL import Image

BASE = "http://127.0.0.1:8188"

def upload_image(path):
    with open(path, "rb") as f:
        data = f.read()
    fname = os.path.basename(path)
    boundary = "----WebKitFormBoundary" + uuid.uuid4().hex
    body = b""
    body += f"--{boundary}\r\n".encode()
    body += f'Content-Disposition: form-data; name="image"; filename="{fname}"\r\n'.encode()
    body += b"Content-Type: image/png\r\n\r\n"
    body += data + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    req = urllib.request.Request(f"{BASE}/upload/image", data=body, headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req) as r:
        resp = json.loads(r.read())
    return resp["name"]

# 生成镜像尾帧
src = r'D:\ComfyUI-New\input\rocket.png'
flip = r'D:\ComfyUI-New\input\rocket_flipped.png'
Image.open(src).transpose(Image.FLIP_LEFT_RIGHT).save(flip)
print("镜像尾帧已生成")

PROMPT = ("a small rocket model as a desktop ornament, "
          "the rocket starts in the exact pose of the first frame and "
          "smoothly rotates 180 degrees on a turntable to reach the exact pose of the last frame, "
          "bright even studio lighting, the background stays pure white for the entire video, "
          "no shadows, no dark areas, no gradient, no text no watermark, "
          "detailed miniature space rocket, silver metallic body, red nose cone and fins, product showcase")

def build_workflow(first_img, last_img):
    return {
        "1": {"class_type": "UNETLoader", "inputs": {
            "unet_name": "minimax_h3_ref2va_pruned_int8_convrot.safetensors", "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {
            "clip_name": "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors", "type": "minimax"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": "minimax_h3_video_vae_fp16.safetensors"}},
        "4": {"class_type": "LoadImage", "inputs": {"image": first_img}},
        "5": {"class_type": "LoadImage", "inputs": {"image": last_img}},
        "6": {"class_type": "LoraLoaderModelOnly", "inputs": {
            "model": ["1", 0],
            "lora_name": "minimax_h3_turbo_v4_step600_ema_pruned_comfyui.safetensors",
            "strength_model": 1.0}},
        "7": {"class_type": "MiniMaxH3ImageToVideo", "inputs": {
            "clip": ["2", 0], "vae": ["3", 0], "prompt": PROMPT,
            "width": 1344, "height": 768, "length": 192,
            "first_frame": ["4", 0], "last_frame": ["5", 0]}},
        "8": {"class_type": "BasicGuider", "inputs": {
            "model": ["6", 0], "conditioning": ["7", 0]}},
        "9": {"class_type": "RandomNoise", "inputs": {"noise_seed": 888}},
        "10": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "euler"}},
        "11": {"class_type": "BasicScheduler", "inputs": {
            "model": ["6", 0], "scheduler": "beta", "steps": 8, "denoise": 1.0}},
        "12": {"class_type": "SamplerCustomAdvanced", "inputs": {
            "noise": ["9", 0], "guider": ["8", 0], "sampler": ["10", 0],
            "sigmas": ["11", 0], "latent_image": ["7", 1]}},
        "13": {"class_type": "VAEDecode", "inputs": {"samples": ["12", 0], "vae": ["3", 0]}},
        "14": {"class_type": "CreateVideo", "inputs": {"images": ["13", 0], "fps": 24}},
        "15": {"class_type": "SaveVideo", "inputs": {
            "video": ["14", 0], "filename_prefix": "rocket_v1", "format": "mp4", "codec": "h264"}},
    }

def submit(prompt_workflow):
    data = json.dumps({"prompt": prompt_workflow, "client_id": str(uuid.uuid4())}).encode()
    req = urllib.request.Request(f"{BASE}/prompt", data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            resp = json.loads(r.read())
        return resp.get("prompt_id")
    except urllib.error.HTTPError as e:
        print("提交失败:", e.read().decode()[:800])
        return None

first = upload_image(src)
last = upload_image(flip)
print("首帧:", first, "| 尾帧:", last)

wf = build_workflow(first, last)
pid = submit(wf)
print("H3 火箭视频任务:", pid)
if pid:
    with open(r"C:\Users\mac\.openclaw\workspace\tmp\rocket_video_pid.txt", "w") as f:
        f.write(pid)
