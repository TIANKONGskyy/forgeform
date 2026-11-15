# -*- coding: utf-8 -*-
"""本地密钥保管：云端 API key 只以**加密**形式落在本机，不出机器。

## 为什么用 Windows DPAPI 而不是自己搓 AES

自搓方案的死结是"加密用的那把主密钥放哪" —— 放同目录等于没加密，放用户
输入等于每次启动都要输密码。DPAPI（`CryptProtectData`）由系统托管主密钥，
加密出来的密文**绑定当前用户 + 当前机器**：拷到别的机器解不开，换个用户也
解不开。这是 Windows 上唯一不用引入第三方库、又真的有用的做法。

非 Windows（或 DPAPI 调用失败）退回 `_Obfuscated`：用机器标识做 HMAC 掩码，
**明确不是安全加密**，只在开发环境用，并在日志里说清楚。

## 存放位置

    .secrets/keys.dat          加密后的 JSON（{name: key}）
    .secrets/issued.json       本机签发的对外 API key（只存 sha256，不存明文）

`.secrets/` 必须在 .gitignore 里 —— 它进 git 就等于把 key 交出去了。

## 用法

    from backend.secrets import put_secret, get_secret, list_secrets, delete_secret
    put_secret("openai", "sk-...")        # 加密落盘
    get_secret("openai")                  # 解密取回（内存里）
    list_secrets()                        # 只回名字和有没有值，不回密钥本身
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets as _secrets_mod
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SECRET_DIR = ROOT / ".secrets"
KEYS_FILE = SECRET_DIR / "keys.dat"
ISSUED_FILE = SECRET_DIR / "issued.json"

# DPAPI 的熵：同一台机器上给不同用途加密时混入，防止一个用途的密文
# 被拿到另一个用途里去解。固定串是刻意的（不保密），作用是域分隔。
_ENTROPY = b"forgeform.zykt.secrets.v1"


# ══════════════════════════════════════════════════════════════════
# Windows DPAPI
# ══════════════════════════════════════════════════════════════════
class _DpapiError(Exception):
    pass


def _dpapi_available() -> bool:
    return sys.platform == "win32"


def _dpapi(data: bytes, protect: bool) -> bytes:
    """CryptProtectData / CryptUnprotectData。失败抛 _DpapiError。"""
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    def _blob(raw: bytes) -> DATA_BLOB:
        buf = ctypes.create_string_buffer(raw, len(raw))
        return DATA_BLOB(len(raw), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))

    blob_in = _blob(data)
    ent = _blob(_ENTROPY)
    blob_out = DATA_BLOB()

    if protect:
        ok = crypt32.CryptProtectData(
            ctypes.byref(blob_in), None, ctypes.byref(ent),
            None, None, 0, ctypes.byref(blob_out))
    else:
        ok = crypt32.CryptUnprotectData(
            ctypes.byref(blob_in), None, ctypes.byref(ent),
            None, None, 0, ctypes.byref(blob_out))
    if not ok:
        raise _DpapiError(f"DPAPI 调用失败，错误码 {ctypes.get_last_error()}")

    try:
        out = ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)
    return out


# ══════════════════════════════════════════════════════════════════
# 非 Windows 兜底（明确不是安全加密）
# ══════════════════════════════════════════════════════════════════
def _machine_key() -> bytes:
    """用机器/用户标识派生一把本地密钥。不是安全边界，只是防"直接肉眼可见"。"""
    parts = [
        os.environ.get("COMPUTERNAME", ""),
        os.environ.get("USERNAME", ""),
        os.environ.get("USERDOMAIN", ""),
        str(Path.home()),
    ]
    return hashlib.sha256(("|".join(parts) + "|forgeform").encode()).digest()


def _xor_stream(data: bytes, key: bytes) -> bytes:
    out = bytearray(len(data))
    pos = 0
    counter = 0
    while pos < len(data):
        block = hashlib.sha256(key + counter.to_bytes(8, "big")).digest()
        for b in block:
            if pos >= len(data):
                break
            out[pos] = data[pos] ^ b
            pos += 1
        counter += 1
    return bytes(out)


# ══════════════════════════════════════════════════════════════════
# 读写
# ══════════════════════════════════════════════════════════════════
def _encrypt(plain: str) -> str:
    raw = plain.encode("utf-8")
    if _dpapi_available():
        return "dpapi:" + base64.b64encode(_dpapi(raw, True)).decode()
    return "mask:" + base64.b64encode(_xor_stream(raw, _machine_key())).decode()


def _decrypt(token: str) -> str:
    token = str(token or "")
    if token.startswith("dpapi:"):
        return _dpapi(base64.b64decode(token[6:]), False).decode("utf-8")
    if token.startswith("mask:"):
        return _xor_stream(base64.b64decode(token[5:]), _machine_key()).decode("utf-8")
    raise ValueError("无法识别的密文格式（可能来自别的机器或已损坏）")


def _load_raw() -> dict:
    try:
        return json.loads(KEYS_FILE.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _save_raw(data: dict) -> None:
    SECRET_DIR.mkdir(parents=True, exist_ok=True)
    tmp = KEYS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(KEYS_FILE)
    try:  # 尽量收紧权限（Windows 上会被忽略，Linux 上有效）
        os.chmod(KEYS_FILE, 0o600)
    except Exception:
        pass


def backend_name() -> str:
    """当前用的加密后端：'dpapi'（Windows 系统托管）或 'mask'（弱兜底）。"""
    return "dpapi" if _dpapi_available() else "mask"


def is_weak() -> bool:
    """True = 当前是非安全兜底，UI 应提示。"""
    return backend_name() == "mask"


def put_secret(name: str, value: str) -> None:
    """加密保存一个 key。空值等于删除。"""
    name = str(name or "").strip()
    if not name:
        raise ValueError("name 不能为空")
    data = _load_raw()
    if value is None or str(value).strip() == "":
        if name in data:
            del data[name]
            _save_raw(data)
        return
    data[name] = {"v": _encrypt(str(value).strip()), "at": _now()}
    _save_raw(data)


def get_secret(name: str) -> str:
    """取回明文 key。解不开返回空串（不抛，避免一个坏 key 挡住整条链路）。"""
    try:
        rec = _load_raw().get(str(name or "").strip())
        if not rec:
            return ""
        return _decrypt(rec.get("v") or "")
    except Exception as exc:
        print(f"[secrets] 取 {name} 失败: {type(exc).__name__}: {exc}")
        return ""


def has_secret(name: str) -> bool:
    return bool(get_secret(name))


def list_secrets() -> list[dict]:
    """列出已保存的 key：**只给名字、时间和掩码尾号，绝不回明文**。"""
    out = []
    for name, rec in (_load_raw() or {}).items():
        tail = ""
        try:
            plain = _decrypt(rec.get("v") or "")
            tail = ("…" + plain[-4:]) if len(plain) > 8 else "…"
        except Exception:
            tail = "(解不开)"
        out.append({"name": name, "at": rec.get("at") or "", "tail": tail})
    return sorted(out, key=lambda r: r["name"])


def delete_secret(name: str) -> bool:
    data = _load_raw()
    if name in data:
        del data[name]
        _save_raw(data)
        return True
    return False


def _now() -> str:
    import time
    return time.strftime("%Y-%m-%d %H:%M")


# ══════════════════════════════════════════════════════════════════
# 对外签发的 API key
# ══════════════════════════════════════════════════════════════════
# 安全约束（这几条是这套东西能不能交给别人的底线）：
#   ① **明文永不落盘** —— 只存 sha256。256 bit 随机串不需要慢哈希，
#      sha256 就够（字典攻击对 32 字节随机数无意义）。
#   ② **整个签发库也要加密** —— 虽然只有哈希，但哈希库被人拿走就能离线比对；
#      用 DPAPI 封一层，拷到别的机器等于废文件。
#   ③ **每条 key 有独立 id** —— 删除/撤销按 id 而不是 label。按 label 撤销是
#      灾难：两个人 label 都叫 "test"，删一个等于把另一个也弄没了。
#   ④ **可设有效期** —— 默认 90 天，到期自动失效；`ttl_days=0` 才永久。
#   ⑤ **每次调用写审计** —— 只记 key id + 路径 + 状态码 + IP，**不记 token**。
#   ⑥ **撤销立刻生效** —— 校验时读库，没有缓存，删掉就 401。

ISSUED_FILE = SECRET_DIR / "issued.dat"
AUDIT_DIR = ROOT / "logs"
DEFAULT_TTL_DAYS = 90


def _load_issued() -> list:
    """读签发库（DPAPI 加密）。坏文件当空库，不抛。"""
    try:
        raw = ISSUED_FILE.read_text(encoding="utf-8")
        if not raw.strip():
            return []
        data = json.loads(raw)
        if isinstance(data, dict) and "enc" in data:
            data = json.loads(_decrypt(data["enc"]))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_issued(data: list) -> None:
    SECRET_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"enc": _encrypt(json.dumps(data, ensure_ascii=False)),
               "v": 1}
    tmp = ISSUED_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(ISSUED_FILE)
    try:
        os.chmod(ISSUED_FILE, 0o600)
    except Exception:
        pass



def _is_expired(exp: str) -> bool:
    """'2026-11-15 19:30' 是否已过。格式坏就当没过期（宁可放行也不误杀）。"""
    try:
        return time.strptime(exp, "%Y-%m-%d %H:%M") < time.localtime()
    except Exception:
        return False


def _new_key_id() -> str:
    return "k_" + _secrets_mod.token_hex(4)


def issue_api_key(label: str = "default", ttl_days: int = DEFAULT_TTL_DAYS) -> dict:
    """签发一把给外部调用者的 key。

    返回 {"token": 明文（只此一次）, "id", "label", "expires_at"}。
    """
    token = "ff_" + _secrets_mod.token_urlsafe(32)
    kid = _new_key_id()
    exp = ""
    if ttl_days and int(ttl_days) > 0:
        exp = time.strftime("%Y-%m-%d %H:%M",
                            time.localtime(time.time() + int(ttl_days) * 86400))
    rec = {"id": kid,
           "label": str(label or "default")[:60],
           "sha256": hashlib.sha256(token.encode()).hexdigest(),
           "at": _now(), "expires_at": exp,
           "calls": 0, "last_at": "", "last_ip": "", "revoked": False}
    data = _load_issued()
    data.append(rec)
    _save_issued(data)
    return {"token": token, "id": kid, "label": rec["label"], "expires_at": exp}


def verify_api_key(token: str, ip: str = "") -> dict | None:
    """校验 key。命中返回记录（累加计数），否则 None。

    ⚠️ 返回的记录里**不含 sha256**，避免被上层顺手写进日志。
    """
    if not token:
        return None
    digest = hashlib.sha256(str(token).encode()).hexdigest()
    data = _load_issued()
    for rec in data:
        stored = str(rec.get("sha256") or "")
        if not stored:
            continue
        if _secrets_mod.compare_digest(stored, digest):
            if rec.get("revoked"):
                return None
            exp = str(rec.get("expires_at") or "")
            if exp and _is_expired(exp):
                return None
            rec["calls"] = int(rec.get("calls") or 0) + 1
            rec["last_at"] = _now()
            if ip:
                rec["last_ip"] = str(ip)[:64]
            _save_issued(data)
            return {k: rec.get(k) for k in
                    ("id", "label", "at", "expires_at", "calls", "last_at", "last_ip")}
    return None


def list_api_keys() -> list[dict]:
    """已签发的 key（无明文、无哈希，只有元信息）。"""
    out = []
    for rec in _load_issued():
        exp = str(rec.get("expires_at") or "")
        expired = bool(exp) and _is_expired(exp)
        out.append({
            "id": rec.get("id") or "",
            "label": rec.get("label") or "",
            "at": rec.get("at") or "",
            "expires_at": exp,
            "expired": expired,
            "revoked": bool(rec.get("revoked")),
            "calls": int(rec.get("calls") or 0),
            "last_at": rec.get("last_at") or "",
            "last_ip": rec.get("last_ip") or "",
        })
    out.sort(key=lambda r: (r["revoked"], r["expired"], r["at"]), reverse=False)
    return out


def revoke_api_key(key_id: str = "", label: str = "") -> int:
    """撤销（保留记录，便于审计）。按 id 优先，label 仅作兼容。"""
    data = _load_issued()
    n = 0
    for rec in data:
        if key_id and rec.get("id") == key_id:
            if not rec.get("revoked"):
                rec["revoked"] = True
                n += 1
        elif label and not key_id and rec.get("label") == label and not rec.get("revoked"):
            rec["revoked"] = True
            n += 1
    if n:
        _save_issued(data)
    return n


def delete_api_key(key_id: str) -> bool:
    """彻底删掉一条 key 记录（用户要"删除"，不是只标记）。"""
    data = _load_issued()
    kept = [r for r in data if r.get("id") != key_id]
    if len(kept) == len(data):
        return False
    _save_issued(kept)
    return True


def delete_all_api_keys() -> int:
    """清空所有签发记录（换机器、或怀疑泄露时用）。"""
    n = len(_load_issued())
    if n:
        _save_issued([])
    return n


# ── 审计 ──────────────────────────────────────────────────────
def audit(key_id: str, method: str, path: str, status: int, ip: str = "",
          note: str = "") -> None:
    """记一条调用。**绝不写 token 本身**，只写 key id。"""
    try:
        AUDIT_DIR.mkdir(parents=True, exist_ok=True)
        f = AUDIT_DIR / f"api-{time.strftime('%Y-%m-%d')}.jsonl"
        rec = {"ts": round(time.time(), 1), "at": _now(),
               "key": key_id or "-", "method": method, "path": path[:120],
               "status": int(status), "ip": str(ip)[:64]}
        if note:
            rec["note"] = note[:200]
        with open(f, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass
