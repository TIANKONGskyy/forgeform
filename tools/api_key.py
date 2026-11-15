# -*- coding: utf-8 -*-
"""管理本机对外 API 的 key（签发 / 列表 / 撤销 / 删除）。

    PYTHONPATH= python tools/api_key.py issue --label 同学A --ttl 30
    PYTHONPATH= python tools/api_key.py list
    PYTHONPATH= python tools/api_key.py revoke --id k_1a2b3c4d
    PYTHONPATH= python tools/api_key.py delete --id k_1a2b3c4d
    PYTHONPATH= python tools/api_key.py delete-all
    PYTHONPATH= python tools/api_key.py crypto

安全约定：
  · **明文只在 issue 那一次打印**，之后只存 sha256（整个库还用 DPAPI 加密）
  · 撤销 = 立刻 401，但保留记录（便于查是谁调用过）
  · 删除 = 记录彻底消失，不可恢复
  · 所有操作按 **id**，不按 label（label 重名时按 label 删会误伤别人）
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend import secrets as S  # noqa: E402


def _print_list(rows: list[dict]) -> None:
    if not rows:
        print("还没有签发过 key。用 issue 创建一把。")
        return
    print(f"{'id':<12}{'label':<16}{'状态':<8}{'签发':<18}{'过期':<18}{'调用':<6}最后来源")
    for r in rows:
        state = "已撤销" if r["revoked"] else ("已过期" if r["expired"] else "有效")
        print(f"{r['id']:<12}{r['label'][:14]:<16}{state:<8}"
              f"{r['at']:<18}{(r['expires_at'] or '永久'):<18}"
              f"{r['calls']:<6}{r['last_ip'] or '—'}")


def main() -> int:
    ap = argparse.ArgumentParser(description="铸想 本机 API key 管理")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_issue = sub.add_parser("issue", help="签发一把新 key")
    p_issue.add_argument("--label", default="default", help="给谁用的")
    p_issue.add_argument("--ttl", type=int, default=S.DEFAULT_TTL_DAYS,
                         help=f"有效天数（默认 {S.DEFAULT_TTL_DAYS}，0=永久）")

    sub.add_parser("list", help="列出所有 key")

    p_revoke = sub.add_parser("revoke", help="撤销（保留记录）")
    p_revoke.add_argument("--id", default="", help="key id（k_xxx）")
    p_revoke.add_argument("--label", default="", help="按 label（兼容旧用法，慎用）")

    p_del = sub.add_parser("delete", help="彻底删除一条记录")
    p_del.add_argument("--id", required=True)

    sub.add_parser("delete-all", help="清空所有 key（换机/怀疑泄露时用）")
    sub.add_parser("crypto", help="看加密后端")

    args = ap.parse_args()

    if args.cmd == "issue":
        r = S.issue_api_key(args.label, ttl_days=args.ttl)
        print("已签发（明文只显示这一次，请立刻存好）：")
        print()
        print("  " + r["token"])
        print()
        print(f"  id        {r['id']}")
        print(f"  有效期至  {r['expires_at'] or '永久'}")
        print()
        print("用法： Authorization: Bearer " + r["token"])
        print(f"删除： PYTHONPATH= python tools/api_key.py delete --id {r['id']}")
        return 0

    if args.cmd == "list":
        _print_list(S.list_api_keys())
        return 0

    if args.cmd == "revoke":
        if not args.id and not args.label:
            print("要给 --id（或 --label）")
            return 1
        n = S.revoke_api_key(key_id=args.id, label=args.label)
        print(f"撤销 {n} 把" if n else "没有匹配的 key")
        return 0

    if args.cmd == "delete":
        print("已删除" if S.delete_api_key(args.id) else "没有这个 id")
        return 0

    if args.cmd == "delete-all":
        n = S.delete_all_api_keys()
        print(f"清空 {n} 把")
        return 0

    if args.cmd == "crypto":
        print(f"加密后端: {S.backend_name()}")
        if S.is_weak():
            print("⚠️ 非安全兜底（mask）——只防肉眼直接看见，不是真加密。")
        else:
            print("DPAPI：密文绑定当前用户 + 当前机器，拷到别的机器解不开。")
        print(f"密钥文件: {S.KEYS_FILE}")
        print(f"签发库  : {S.ISSUED_FILE}（DPAPI 整体加密）")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
