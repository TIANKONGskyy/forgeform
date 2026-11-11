#!/usr/bin/env python3
"""调用本地 Qwen3.8-27B via llama-server (端口8080)"""
import urllib.request
import json
import sys
import os

LLAMA_SERVER = "http://localhost:8080/v1/chat/completions"
MODEL = "qwen3.8"  # llama-server加载时自动识别

def chat(prompt, system="", max_tokens=4096, temperature=0.7):
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    data = {
        "model": MODEL,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": False
    }

    req = urllib.request.Request(
        LLAMA_SERVER,
        data=json.dumps(data).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            result = json.load(resp)
            return result["choices"][0]["message"]["content"]
    except Exception as e:
        return f"Error: {e}"

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python llama_chat.py <prompt> [system_prompt]")
        sys.exit(1)
    prompt = sys.argv[1]
    system = sys.argv[2] if len(sys.argv) > 2 else ""
    result = chat(prompt, system)
    print(result)
