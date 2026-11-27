"""Pure-PyTorch stand-in for the three flash-attn entry points Direct3D-V3 uses.

Why this file exists
--------------------
``direct3d/models/sparse_atten.py`` imports ``flash_attn`` and calls

    flash_attn.flash_attn_varlen_kvpacked_func(q, kv, cu_seqlens_q, cu_seqlens_kv, max_q, max_kv)
    flash_attn.flash_attn_qkvpacked_func(qkv)
    flash_attn.flash_attn_varlen_qkvpacked_func(qkv, cu_seqlens, max_seqlen)

flash-attn 2 has no sm_70 kernels (Volta is dropped), so on this V100 the package cannot be
built.  Everything below reproduces the same semantics with
``torch.nn.functional.scaled_dot_product_attention``, which on sm_70 dispatches to the
cutlass memory-efficient kernel (flash and cuDNN backends are unavailable; the math backend
would materialise the N x N matrix, so we pin the efficient backend and fall back only if
the shape really is unsupported).

Shapes follow flash-attn exactly:

    qkvpacked          qkv [B, N, 3, H, D]                       -> [B, N, H, D]
    varlen_qkvpacked   qkv [T, 3, H, D]  + cu_seqlens            -> [T, H, D]
    varlen_kvpacked    q [Tq, H, D], kv [Tk, 2, H, D] + 2 cu     -> [Tq, H, D]

Everything is fp16-first (V100 has no fast bf16 and no fp8), but fp32/bf16 inputs are also
accepted; accumulation happens in fp32 inside SDPA regardless of input dtype.

Varlen strategy
---------------
flash-attn packs sequences back-to-back and never pads.  The literal translation would be
"pad to max(seqlen) + attn_mask", which is what ``_varlen_padded`` does -- but in this model a
window can hold up to ``window_size**3`` tokens (32768 at the 512 stage, 8 windows), so a
padded mask would cost 8 x 32768 x 32768 bool = 8 GiB before any attention math.  The default
path therefore slices each sequence out of the packed tensor and issues one SDPA call per
sequence (``window`` count is 8 at 512, 8 at 1024, 27 at 1536 -- a cheap Python loop), which is
exactly equivalent and keeps memory linear in tokens.  Set ``FLASH_ATTN_COMPAT_PADMASK=1`` to
force the padded-mask path (useful as a cross-check; see ``test_compat.py``).

The model itself has no use for causal masks, dropout, alibi slopes, softcap or returning
attention probabilities; those arguments exist so the call sites keep working, and the ones
we cannot honour raise ``NotImplementedError`` instead of silently changing results.
"""

from __future__ import annotations

import os
import warnings

import torch
import torch.nn.functional as F

__version__ = "2.8.3+compat.v100"

#: Force the padded-mask varlen implementation (cross-check / debugging).
PAD_MASK_MODE = os.environ.get("FLASH_ATTN_COMPAT_PADMASK", "0") not in ("0", "", "false", "False")

#: Above this many padding-mask bytes the padded path refuses and falls back to loops.
PAD_MASK_BYTE_LIMIT = 1 << 30

_EFFICIENT_BACKEND = None  # None = not probed, True/False = probe result


def _probe_efficient_backend():
    """One-time probe: can sm_70 use the cutlass memory-efficient SDPA kernel?"""
    global _EFFICIENT_BACKEND
    if _EFFICIENT_BACKEND is not None:
        return _EFFICIENT_BACKEND
    try:
        from torch.nn.attention import SDPBackend, sdpa_kernel

        probe = torch.randn(1, 2, 8, 16, device="cuda", dtype=torch.float16)
        with sdpa_kernel([SDPBackend.EFFICIENT_ATTENTION]):
            F.scaled_dot_product_attention(probe, probe, probe)
        _EFFICIENT_BACKEND = True
    except Exception:  # pragma: no cover - depends on GPU/torch build
        _EFFICIENT_BACKEND = False
    return _EFFICIENT_BACKEND


def _sdpa_context(device):
    """Fresh context manager per call: sdpa_kernel objects are single-use.

    The pinned backend only makes sense on CUDA; CPU tensors keep torch's default dispatch
    (math kernel), otherwise sdpa_kernel would disable the only backend available there.
    """
    from contextlib import nullcontext

    if not torch.device(device).type == "cuda":
        return nullcontext()
    if _probe_efficient_backend():
        from torch.nn.attention import SDPBackend, sdpa_kernel

        return sdpa_kernel([SDPBackend.EFFICIENT_ATTENTION])
    return nullcontext()


def _sdpa(q, k, v, softmax_scale=None, causal=False, dropout_p=0.0):
    """q, k, v: [B, H, L, D] -> [B, H, L, D]."""
    scale = softmax_scale if softmax_scale is not None else q.shape[-1] ** -0.5
    kwargs = dict(attn_mask=None, is_causal=bool(causal), scale=float(scale))
    if dropout_p:
        kwargs["dropout_p"] = float(dropout_p)
    with _sdpa_context(q.device):
        return F.scaled_dot_product_attention(q, k, v, **kwargs)


def _check_unsupported(softcap, alibi_slopes, return_attn_probs):
    if softcap:
        raise NotImplementedError("flash_attn_compat does not implement softcap")
    if alibi_slopes is not None:
        raise NotImplementedError("flash_attn_compat does not implement alibi_slopes")
    if return_attn_probs:
        raise NotImplementedError("flash_attn_compat does not return attention probabilities")


def _ranges(cu_seqlens):
    if cu_seqlens is None:
        return None
    if isinstance(cu_seqlens, torch.Tensor):
        values = cu_seqlens.detach().to("cpu", torch.int64).tolist()
    else:
        values = [int(v) for v in cu_seqlens]
    return list(zip(values[:-1], values[1:]))


def _heads_first(x):
    """[L, H, D] -> [1, H, L, D] (contiguous)."""
    return x.transpose(0, 1).unsqueeze(0).contiguous()


def _heads_last(x):
    """[1, H, L, D] -> [L, H, D]."""
    return x.squeeze(0).transpose(0, 1)


def _varlen_padded(q, k, v, cu_q, cu_k, softmax_scale, causal, dropout_p):
    """Literal flash-attn varlen emulation: pad to max length, mask the padding."""
    rq, rk = _ranges(cu_q), _ranges(cu_k)
    batch = len(rq)
    device = q.device
    max_q = max(stop - start for start, stop in rq)
    max_k = max(stop - start for start, stop in rk)
    nbytes = batch * max_q * max_k * (1 if q.dtype == torch.bool else 4)
    if nbytes > PAD_MASK_BYTE_LIMIT:
        return None
    qp = q.new_zeros(batch, max_q, *q.shape[1:])
    kp = k.new_zeros(batch, max_k, *k.shape[1:])
    vp = v.new_zeros(batch, max_k, *v.shape[1:])
    mask = torch.zeros(batch, 1, max_q, max_k, dtype=torch.bool, device=device)
    for i, ((qs, qe), (ks, ke)) in enumerate(zip(rq, rk)):
        qp[i, : qe - qs] = q[qs:qe]
        kp[i, : ke - ks] = k[ks:ke]
        vp[i, : ke - ks] = v[ks:ke]
        mask[i, :, : qe - qs, : ke - ks] = True
    scale = float(softmax_scale) if softmax_scale is not None else q.shape[-1] ** -0.5
    out = F.scaled_dot_product_attention(
        qp.transpose(1, 2),
        kp.transpose(1, 2),
        vp.transpose(1, 2),
        attn_mask=mask,
        is_causal=bool(causal),
        scale=scale,
        dropout_p=float(dropout_p) if dropout_p else 0.0,
    )
    # Fully masked query rows (padding) come back as NaN on some backends.
    out = torch.nan_to_num(out, nan=0.0)
    pieces = [out[i, :, : qe - qs].transpose(0, 1) for i, (qs, qe) in enumerate(rq)]
    return torch.cat(pieces, dim=0) if pieces else q.new_zeros(0, *q.shape[1:])


def _varlen_loop(q, k, v, cu_q, cu_k, softmax_scale, causal, dropout_p):
    """One SDPA call per packed sequence -- exact, and memory stays O(tokens)."""
    rq, rk = _ranges(cu_q), _ranges(cu_k)
    pieces = []
    for (qs, qe), (ks, ke) in zip(rq, rk):
        if qe <= qs:
            continue
        if ke <= ks:  # no keys for this query group; flash-attn would not be called either
            pieces.append(q.new_zeros(qe - qs, *q.shape[1:]))
            continue
        o = _sdpa(
            _heads_first(q[qs:qe]),
            _heads_first(k[ks:ke]),
            _heads_first(v[ks:ke]),
            softmax_scale=softmax_scale,
            causal=causal,
            dropout_p=dropout_p,
        )
        pieces.append(_heads_last(o))
    if not pieces:
        return q.new_zeros(0, *q.shape[1:])
    return torch.cat(pieces, dim=0)


def _varlen(q, k, v, cu_q, cu_k, softmax_scale, causal, dropout_p, prefer_pad_mask):
    if prefer_pad_mask:
        out = _varlen_padded(q, k, v, cu_q, cu_k, softmax_scale, causal, dropout_p)
        if out is not None:
            return out
    return _varlen_loop(q, k, v, cu_q, cu_k, softmax_scale, causal, dropout_p)


def flash_attn_qkvpacked_func(
    qkv,
    dropout_p=0.0,
    softmax_scale=None,
    causal=False,
    window_size=(-1, -1),
    softcap=0.0,
    alibi_slopes=None,
    deterministic=False,
    return_attn_probs=False,
):
    """qkv [B, N, 3, H, D] -> [B, N, H, D]."""
    _check_unsupported(softcap, alibi_slopes, return_attn_probs)
    q, k, v = qkv.unbind(dim=2)
    # [B, N, H, D] -> [B, H, N, D]
    q, k, v = (t.transpose(1, 2).contiguous() for t in (q, k, v))
    out = _sdpa(q, k, v, softmax_scale=softmax_scale, causal=causal, dropout_p=dropout_p)
    return out.transpose(1, 2).contiguous()


def flash_attn_varlen_qkvpacked_func(
    qkv,
    cu_seqlens,
    max_seqlen,
    dropout_p=0.0,
    softmax_scale=None,
    causal=False,
    window_size=(-1, -1),
    softcap=0.0,
    alibi_slopes=None,
    deterministic=False,
    return_attn_probs=False,
):
    """qkv [T, 3, H, D] + cu_seqlens -> [T, H, D]."""
    _check_unsupported(softcap, alibi_slopes, return_attn_probs)
    q, k, v = qkv.unbind(dim=1)
    return _varlen(
        q, k, v, cu_seqlens, cu_seqlens, softmax_scale, causal, dropout_p,
        prefer_pad_mask=PAD_MASK_MODE,
    )


def flash_attn_varlen_kvpacked_func(
    q,
    kv,
    cu_seqlens_q,
    cu_seqlens_k,
    max_seqlen_q,
    max_seqlen_k,
    dropout_p=0.0,
    softmax_scale=None,
    causal=False,
    window_size=(-1, -1),
    softcap=0.0,
    alibi_slopes=None,
    deterministic=False,
    return_attn_probs=False,
):
    """q [Tq, H, D], kv [Tk, 2, H, D] -> [Tq, H, D].  cu_seqlens_kv may be cu_seqlens_q."""
    _check_unsupported(softcap, alibi_slopes, return_attn_probs)
    if kv.dim() == 5:
        # CrossAttention passes kv as [B, S, 2, H, D] whenever layout_kv is given, which the
        # real flash-attn varlen entry cannot take either; flattening is the only interpretation
        # that matches the matching cu_seqlens_kv (B groups of S keys).
        kv = kv.flatten(0, 1)
    k, v = kv.unbind(dim=1)
    return _varlen(
        q, k, v, cu_seqlens_q, cu_seqlens_k, softmax_scale, causal, dropout_p,
        prefer_pad_mask=PAD_MASK_MODE,
    )


def flash_attn_func(
    q,
    k,
    v,
    dropout_p=0.0,
    softmax_scale=None,
    causal=False,
    window_size=(-1, -1),
    softcap=0.0,
    alibi_slopes=None,
    deterministic=False,
    return_attn_probs=False,
):
    """q/k/v [B, L, H, D] -> [B, L, H, D]."""
    _check_unsupported(softcap, alibi_slopes, return_attn_probs)
    out = _sdpa(
        q.transpose(1, 2).contiguous(),
        k.transpose(1, 2).contiguous(),
        v.transpose(1, 2).contiguous(),
        softmax_scale=softmax_scale,
        causal=causal,
        dropout_p=dropout_p,
    )
    return out.transpose(1, 2).contiguous()


def flash_attn_varlen_func(
    q,
    k,
    v,
    cu_seqlens_q,
    cu_seqlens_k,
    max_seqlen_q,
    max_seqlen_k,
    dropout_p=0.0,
    softmax_scale=None,
    causal=False,
    window_size=(-1, -1),
    softcap=0.0,
    alibi_slopes=None,
    deterministic=False,
    return_attn_probs=False,
):
    """q [Tq, H, D], k/v [Tk, H, D] -> [Tq, H, D]."""
    _check_unsupported(softcap, alibi_slopes, return_attn_probs)
    return _varlen(
        q, k, v, cu_seqlens_q, cu_seqlens_k, softmax_scale, causal, dropout_p,
        prefer_pad_mask=PAD_MASK_MODE,
    )


def flash_attn_kvpacked_func(
    q,
    kv,
    dropout_p=0.0,
    softmax_scale=None,
    causal=False,
    window_size=(-1, -1),
    softcap=0.0,
    alibi_slopes=None,
    deterministic=False,
    return_attn_probs=False,
):
    """q [B, L, H, D], kv [B, L, 2, H, D] -> [B, L, H, D]."""
    _check_unsupported(softcap, alibi_slopes, return_attn_probs)
    k, v = kv.unbind(dim=2)
    return flash_attn_func(
        q, k, v, dropout_p=dropout_p, softmax_scale=softmax_scale, causal=causal,
    )


def is_available() -> bool:
    """Mirrors ``flash_attn.is_available`` for code that probes it."""
    return torch.cuda.is_available()


def __getattr__(name):  # pragma: no cover - guards against API drift
    if name.startswith("flash_attn"):
        raise AttributeError(f"flash_attn_compat has no attribute {name!r}")
    raise AttributeError(name)


def _warn_once():
    warnings.warn(
        "flash_attn -> flash_attn_compat (pure PyTorch SDPA); fp16 only on sm_70",
        stacklevel=2,
    )


__all__ = [
    "flash_attn_func",
    "flash_attn_kvpacked_func",
    "flash_attn_qkvpacked_func",
    "flash_attn_varlen_func",
    "flash_attn_varlen_kvpacked_func",
    "flash_attn_varlen_qkvpacked_func",
    "is_available",
]
