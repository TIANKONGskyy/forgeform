#!/usr/bin/env python3
"""本地 Qwen3.8 深度调研：AI 3D打印定制市场"""
import urllib.request
import json
import os
from datetime import datetime

LLAMA = "http://localhost:8080/v1/chat/completions"
MODEL = "qwen3.8"
TRACKING_FILE = os.path.expanduser("~/AI_3D市场动态_持续追踪.md")

def local_llm(prompt, system=""):
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    data = {
        "model": MODEL,
        "messages": messages,
        "max_tokens": 4096,
        "temperature": 0.5,
        "stream": False
    }
    req = urllib.request.Request(
        LLAMA,
        data=json.dumps(data).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        result = json.load(resp)
        return result["choices"][0]["message"]["content"]

SYSTEM_PROMPT = """你是一个专业的AI和3D打印行业研究员。分析市场动态时，给出具体数字（价格/转化率/成本），不要模糊表述。输出中文。"""

def main():
    date = datetime.now().strftime("%Y-%m-%d")
    print(f"[{date}] 开始本地模型调研...")

    queries = [
        "分析当前国内AI 3D打印定制服务的市场价格区间（闲鱼/淘宝），给出具体数字。",
        "分析3D打印代工厂（嘉立创、魔猴网等）的最新报价，含材质、单价、交期。",
        "分析小红书上3D打印定制的获客策略和爆款案例，给出具体转化数据。",
        "对比竞品Meshy、Tripo3D的最新定价和功能差异，给出具体数字。",
        "分析AI生成3D内容的最新法规风险，给出具体法规名称和要点。",
    ]

    report = f"\n## 本地模型调研补充（{date}）\n\n"
    for i, q in enumerate(queries, 1):
        print(f"  维度{i}/{len(queries)}...")
        answer = local_llm(q, SYSTEM_PROMPT)
        report += f"### 维度{i}\n{answer}\n\n"

    # 读取现有文件
    existing = ""
    if os.path.exists(TRACKING_FILE):
        with open(TRACKING_FILE, "r", encoding="utf-8") as f:
            existing = f.read()

    # 追加新章节
    with open(TRACKING_FILE, "w", encoding="utf-8") as f:
        # 更新摘要行
        lines = existing.split("\n")
        new_lines = []
        for line in lines:
            new_lines.append(line)
        # 确保文件以\n结尾
        if existing and not existing.endswith("\n"):
            report = "\n" + report
        f.write(existing + report)

    print(f"✅ 调研完成，追加到 {TRACKING_FILE}")
    return report

if __name__ == "__main__":
    main()
