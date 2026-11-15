# -*- coding: utf-8 -*-
"""管理本机对外 API 的 key。

    PYTHONPATH= python tools/api_key.py issue --label 同学A
    PYTHONPATH= python tools/api_key.py list
    PYTHONPATH= python tools/api_key.py revoke --label 同学A

**明文只在 issue 那一次打印出来**，之后就只剩 sha256。丢了就重新签发一把
（并 revoke 旧的），不要试图"找回"。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend import secrets as S  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="铸想 本机 API key 管理")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_issue = sub.add_parser("issue", help="签发一把新 key")
    p_issue.add_argument("--label", default="default", help="给谁用的（便于撤销）")

    sub.add_parser("list", help="列出已签发的 key")

    p_revoke = sub.add_parser("revoke", help="按 label 撤销")
    p_revoke.add_argument("--label", required=True)

    sub.add_parser("crypto", help="看密钥加密用的是什么后端")

    args = ap.parse_args()

    if args.cmd == "issue":
        tok = S.issue_api_key(args.label)
        print("已签发（明文只显示这一次，请立刻存好）：")
        print()
        print("  " + tok)
        print()
        print("用法： Authorization: Bearer " + tok)
        return 0

    if args.cmd == "list":
        rows = S.list_api_keys()
        if not rows:
            print("还没有签发过 key。用 issue 创建一把。")
            return 0
        print(f"{'label':<18}{'签发时间':<20}{'调用次数':<10}{'最近调用'}")
        for r in rows:
            print(f"{r.get('label', ''):<18}{r.get('at', ''):<20}"
                  f"{r.get('calls', 0):<10}{r.get('last_at', '') or '—'}")
        return 0

    if args.cmd == "revoke":
        n = S.revoke_api_key(args.label)
        print(f"撤销 {n} 把（label={args.label}）" if n else "没有匹配的 label")
        return 0

    if args.cmd == "crypto":
        print(f"加密后端: {S.backend_name()}")
        if S.is_weak():
            print("⚠️ 当前是非安全兜底（mask）。只防肉眼直接看见，不是真加密。")
            print("   Windows 上会自动用 DPAPI；这个提示出现在非 Windows 环境。")
        else:
            print("DPAPI：密文绑定当前用户 + 当前机器，拷到别的机器解不开。")
        print(f"密钥文件: {S.KEYS_FILE}")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
