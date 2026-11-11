# -*- coding: utf-8 -*-
"""注入天枢主题开机动画 v4 到 app.py（2026-08-11）"""
import re
import sys

sys.stdout.reconfigure(encoding="utf-8")

p = r"D:\AI-Workshop\app.py"
t = open(p, encoding="utf-8").read()

# 读取生成的 JS（gen_boot_v4.py 输出）
import subprocess
out = subprocess.run(
    [sys.executable, r"D:\AI-Workshop\scripts\gen_boot_v4.py"],
    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
)
JS = out.stdout.strip()

# 找到当前 Blocks(head=r'''...''') 块
m = re.search(r"with gr\.Blocks\(title=\"AI 创意工坊\", css=CSS, head=r'''(.*?)'''\) as app:", t, re.S)
if not m:
    print("ERROR: head block not found")
    sys.exit(1)

new_head = "r'''" + JS.replace("'''", "\\'\\'\\'") + "'''"
t = t[:m.start()] + 'with gr.Blocks(title="AI 创意工坊", css=CSS, head=' + new_head + ') as app:' + t[m.end():]

open(p, "w", encoding="utf-8").write(t)
print("injected v4 boot animation")
