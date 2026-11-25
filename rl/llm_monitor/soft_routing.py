"""Confidence-weighted routing: route gradient to FORGET in proportion to the
judge's P(hack) instead of by a hard flag.

GRAFT's classic routing at strength lam uses, for a flagged sample, the
gradient masks (retain, forget) = (1 - lam, 1 + lam * (kappa_F - 1)) and for an
unflagged sample (1, 1). Here every sample gets its own lam_i = p_i:

    retain_w_i = 1 - p_i
    forget_w_i = 1 + p_i * (kappa_F - 1)

and the advantage baseline is the retain-weighted mean reward of the group
(scale = whole-group std), so the masked retain gradient stays zero-mean. With
p in {0, 1} this is exactly hard-flag GRAFT (lam = 1): masks (1,1)/(0,kappa_F)
and the non-flagged baseline. The caller (advantages.py) hands the masks and
this advantage to the trainer's existing lam != 1 update unchanged: two
backwards per microbatch, the optimizer's second moment taken from the
non-flagged-baseline advantage. Arm 5 therefore costs more per step than arm 4.
"""
from __future__ import annotations

import torch


def soft_routing(raw_rewards: torch.Tensor, p_hack: torch.Tensor, G: int, kappa_f: float):
    """Returns (advantages, retain_w, forget_w), each of shape [n]."""
    from advantages import _baseline_weighted_var_all

    p = p_hack.to(raw_rewards.dtype).clamp(0.0, 1.0)
    retain_w = 1.0 - p
    forget_w = 1.0 + p * (kappa_f - 1.0)
    advantages = _baseline_weighted_var_all(raw_rewards, retain_w, G)
    return advantages, retain_w.to(torch.float32), forget_w.to(torch.float32)
