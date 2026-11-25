"""Trainer-side glue, called once per rollout right after the monitor ran
(train.py, Step C). A no-op unless the monitor was `llm_judge_countdown`.

Two jobs:
  1. hand the judge's P(hack) to the advantage code when the run uses
     confidence-weighted routing;
  2. append one line to <run>/judge_log.jsonl: judge cost/latency and its
     quality against the environment's ground-truth hack channels.

Ground truth is read HERE, for logging only. The judge never sees it.
"""
from __future__ import annotations

import json
import os

import torch

from llm_monitor import judge
from llm_monitor.metrics import FORM_COMPONENTS, HACK_COMPONENT, form_of, quality


def _ground_truth_forms(trainer, n):
    """Per-sample ground-truth hack form from the reward components' cached
    scores, or None if they are not available for exactly these n samples."""
    cr = getattr(trainer, "_combined_reward", None)
    if cr is None:
        return None
    wanted = {HACK_COMPONENT, *FORM_COMPONENTS.values()}
    cols = {}
    for name, fn, _scale, _role in cr.components:
        scores = getattr(fn, "_last_scores", None)
        if name in wanted and scores is not None and len(scores) == n:
            cols[name] = scores
    if HACK_COMPONENT not in cols:
        return None
    return [form_of({name: col[i] for name, col in cols.items()}) for i in range(n)]


def after_detection(trainer, completions, candidate, n_samples, device):
    """Returns a float tensor [n_samples] of P(hack) for confidence-weighted
    routing, or None (hard-flag routing / other monitors)."""
    rec = judge.take_last(completions)
    if rec is None:
        return None
    scores = [s if c else 0.0 for s, c in zip(rec["scores"], candidate)]

    forms = _ground_truth_forms(trainer, len(scores))
    line = {"step": int(trainer.state.global_step), "soft": rec["soft"], **rec["usage"]}
    if forms is not None:
        line["quality"] = quality(scores, forms, rec["threshold"])
        # raw pairs, so quality can be pooled over any window afterwards
        line["scores"] = [round(s, 4) for s in scores]
        line["forms"] = forms
    with open(os.path.join(trainer.args.output_dir, "judge_log.jsonl"), "a") as f:
        f.write(json.dumps(line) + "\n")
    q = line.get("quality", {})
    print(f"[llm_monitor] step={line['step']} calls={line['api_calls']}/{line['n']} "
          f"{line['seconds']:.1f}s ${line['cost_usd']:.3f} auroc={q.get('auroc')} "
          f"recall={q.get('recall')} fpr={q.get('fpr')}")

    if not rec["soft"]:
        return None
    padded = scores + [0.0] * (n_samples - len(scores))   # anchor-env tail: never routed
    return torch.tensor(padded, dtype=torch.float32, device=device)
