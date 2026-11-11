# -*- coding: utf-8 -*-
import urllib.request, json
h = json.load(urllib.request.urlopen('http://127.0.0.1:8188/history', timeout=10))
# history 可能是 dict 或 list
if isinstance(h, dict):
    items = list(h.items())[-8:]
    for pid, entry in items:
        prompt_text = ''
        p = entry.get('prompt', {})
        if isinstance(p, list):
            p = p[0] if p else {}
        if isinstance(p, dict):
            for nid, node in p.items():
                if isinstance(node, dict):
                    ct = node.get('class_type', '')
                    if ct in ('CLIPTextEncodeFlux', 'CLIPTextEncode'):
                        inp = node.get('inputs', {})
                        prompt_text = (inp.get('clip_l') or inp.get('text') or '')[:70]
        status = entry.get('status', {}).get('status_str', '?')
        print(f"{pid[:8]} [{status}] {prompt_text}")
else:
    print("history 是 list，长度", len(h))
    for entry in h[-8:]:
        print(entry)
