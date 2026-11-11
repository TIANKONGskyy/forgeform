"""验证 z_image（z_image_turbo-Q8_0.gguf）到底认不认提示词。

事故现象：
    用户要浣熊 → 图对（qwen21_ref 出的）→ 换了 z_image 之后，
    要浣熊出卡通小男孩、要**大象**也出卡通小男孩。
    提示词完全不起作用。

怀疑：
    Z-Image 是阿里 6B（Lumina2/AuraFlow 架构），文本编码器跟常规 SD/FLUX 不是同一个。
    comfy_client 的 _build_zimage_workflow 用 CLIPLoaderGGUF 挂编码器 ——
    如果挂的不是 Z-Image 配套那个，模型就收不到有效文本条件，
    只会按自己的训练先验输出（可爱 3D 卡通角色）——正好是看到的"小男孩"。

做法：
    绕开铸想整条业务链，用项目自己的 comfy_client 直接打一次 ComfyUI，
    提示词给一个**绝不可能和卡通小男孩混淆**的东西。
    直连还能避免"是不是上游传错提示词"的干扰。

跑法：python scripts/_zimage_probe.py
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from backend.comfy_client import ComfyUIClient  # noqa: E402

# 故意选一个和"卡通人物"毫无关系的物体 —— 出了它才算认提示词
PROMPT = "a red apple on a wooden table, photorealistic, studio lighting, single object"
NEGATIVE = "person, human, cartoon, character, figurine"

cfg = json.load(open("config.json", encoding="utf-8"))
zimage = (cfg.get("models") or {}).get("image", {}).get("z_image") or {}

print("=" * 66)
print("z_image 配置")
print("=" * 66)
for k, v in zimage.items():
    print(f"  {k} = {v}")

print()
print("=" * 66)
print("提交（提示词故意给红苹果）")
print("=" * 66)
print(f"  prompt   = {PROMPT}")
print(f"  negative = {NEGATIVE}")

client = ComfyUIClient(cfg)
if not client.is_running():
    print("\n❌ ComfyUI 没在跑（8188）")
    sys.exit(1)

params = {
    "prompt": PROMPT,
    "negative_prompt": NEGATIVE,
    "model_config": zimage,
    "steps": zimage.get("steps", 8),
    "width": 1024,
    "height": 1024,
    "seed": 12345,  # 固定种子：排除"随机性"这个变量
}

wf = client.build_workflow("image", params)
# 看提示词到底有没有进 workflow（这步能抓"根本没接上"这类问题）
flat = json.dumps(wf, ensure_ascii=False)
print(f"\n  workflow 里含提示词原文 = {PROMPT in flat}")
print(f"  workflow 里含 apple      = {'apple' in flat}")

t0 = time.time()
res = client.submit_workflow(wf, model_config=zimage)
# ★ submit_workflow 返回的可能是**裸 id 字符串**，不是 dict —— 别按 dict 取
pid = res if isinstance(res, str) else (res or {}).get("prompt_id")
print(f"  提交 → prompt_id={pid}")

if not pid:
    print(f"\n❌ 提交失败：{json.dumps(res, ensure_ascii=False)[:400]}")
    sys.exit(1)

out = client.wait_for_completion(pid, timeout=420)
print(f"  完成，耗时 {round(time.time()-t0)}s")

img = client.get_result_image(out) if out else None
print(f"  结果图 = {img}")

if not img:
    print("\n❌ 没拿到图")
    sys.exit(1)

out_path = os.path.abspath("docs/../zimage-probe.jpg")
import shutil  # noqa: E402

shutil.copy(img, out_path)
print(f"\n  ★ 已拷到 {out_path}")
print("    （根目录下，用 vision_analyze 看它是不是红苹果）")
