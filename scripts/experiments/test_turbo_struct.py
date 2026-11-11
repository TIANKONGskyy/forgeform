"""turbo lora 视频工作流结构验证：提交后检查 node_errors，立即 interrupt（不实际跑采样）"""
import sys, time, requests, json
sys.path.insert(0, "D:/AI-Workshop")
from backend.pipeline import Pipeline

p = Pipeline("D:/AI-Workshop/config.json")

# 构建 turbo 视频工作流（含 turbo lora）
params = {
    "model_config": p.config["models"]["video"]["h3"],
    "input_image": "test.png",
    "prompt": "a small robot rotating on turntable",
    "seed": 42,
}
wf = p.comfy._build_video_workflow(params)
prompt = wf["prompt"]

# 检查 turbo lora 节点是否在
has_lora = any(n.get("class_type") == "LoraLoaderModelOnly" for n in prompt.values())
print("Turbo LoRA 节点存在:", has_lora)
# 检查 KSampler steps
ks = [n for n in prompt.values() if n.get("class_type") == "KSampler"][0]
print("KSampler steps:", ks["inputs"]["steps"], "| scheduler:", ks["inputs"]["scheduler"])

# 提交给 ComfyUI，看是否 node_errors（结构验证）
r = requests.post("http://127.0.0.1:8188/prompt", json={"prompt": prompt, "client_id": "turbo_test"}, timeout=30)
result = r.json()
if "node_errors" in result and result["node_errors"]:
    print("❌ 节点错误:", json.dumps(result["node_errors"], ensure_ascii=False)[:2000])
else:
    pid = result.get("prompt_id", "?")
    print(f"✅ 工作流结构验证通过（无 node_errors），prompt_id={pid[:8]}")
    # 立即 interrupt，不实际跑采样（避免温度风险）
    time.sleep(1)
    requests.post("http://127.0.0.1:8188/interrupt", timeout=5)
    print("已 interrupt（未实际采样）")
