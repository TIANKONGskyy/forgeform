# -*- coding: utf-8 -*-
import requests
r = requests.get('http://127.0.0.1:11434/api/tags', timeout=10)
for m in r.json()['models']:
    size_gb = m.get('size', 0) / 1e9
    print(f"  {m['name']:30s} {size_gb:.1f}GB")
