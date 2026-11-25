"""GRAFT with LLM monitors on hf100 Countdown-Code (Qwen3-8B, SFT-primed).

Arms (ARMS=<a>,<b> selects; default all five main arms):
  dn          1. no routing (vanilla GRPO): the paper's No-intervention config
                 (sweeps/countdown_code_rp.py `_base`, lr 5e-4).
  rule        2. GRAFT + rule monitor (test_overwrite only): the paper's GRAFT
                 config (sweeps/countdown_hf100_gr_newinit.py `_ANCHORED`:
                 LeetCode anchor 1:16, classic routing, balanced + split-moment,
                 lr 5e-4/3).
  oracle      3. as `rule`, monitor = ground truth over ALL hack forms.
  judge       4. as `rule`, monitor = LLM judge, hard flag at the frozen threshold.
  judge_soft  5. as `rule`, monitor = LLM judge, confidence-weighted routing.
  oracle_r90 / oracle_r70 / oracle_r50   optional 6. oracle with recall 0.9/0.7/0.5.

Arms 2-6 differ ONLY in the monitor (configs/llmmon_countdown_*.yaml are
generated from the paper's config with the rh_detector block swapped).

Memory: the paper's defaults (H200, 141 GB) unless LLMMON_80GB=1, which applies
upstream's H100 settings (vLLM share 0.55 + gradient checkpointing) to every arm.
PILOT=1 gives the reduced-step pilot (60 steps, dense full-completion
evals for judge validation). SEEDS=9,15 overrides the seeds.

    python -u sweep.py --name llmmon --config sweeps/llmmon_arms.py \\
        --no_baseline --no_wandb --no-eval_at_start
"""
import os

from sweeps.countdown_code_rp import _base
from sweeps.countdown_hf100_gr_newinit import _ANCHORED
from llm_monitor.variants import variant_path

_BOX = {
    # sweeps/countdown_lconly_full.py: 0.45 kills the vLLM server at boot on 80 GB
    **({"vllm_gpu_memory": 0.55, "gradient_checkpointing": True}
       if os.environ.get("LLMMON_80GB", "") == "1" else {}),
    "model": os.environ.get("GRAFT_COUNTDOWN_SFT_MODEL", "output/countdown_sft_model/qwen3-8b"),
    "no_wandb": True,
}

_GRAFT = {**_ANCHORED, **_BOX}

ARMS = {
    "dn": {**_base, **_BOX},
    "rule": _GRAFT,
    "oracle": {**_GRAFT, "config": variant_path("oracle")},
    "judge": {**_GRAFT, "config": variant_path("judge")},
    "judge_soft": {**_GRAFT, "config": variant_path("judge_soft")},
    **{f"oracle_r{int(r * 100)}": {**_GRAFT, "config": variant_path("oracle"),
                                    "rh_detector_recall": r}
       for r in (0.9, 0.7, 0.5)},
}
MAIN_ARMS = ("dn", "rule", "oracle", "judge", "judge_soft")

_PILOT = {  # reduced steps; every eval sample logged in full for judge validation
    "max_steps": 60, "save_steps": 60, "eval_every": 4, "routing_eval_prompts": 128,
}

_arms = [a for a in os.environ.get("ARMS", "").split(",") if a] or list(MAIN_ARMS)
assert not set(_arms) - set(ARMS), f"unknown ARMS {sorted(set(_arms) - set(ARMS))}"
_seeds = [int(s) for s in os.environ.get("SEEDS", "9,15").split(",")]
_pilot = os.environ.get("PILOT", "") == "1"

runs = [
    {**ARMS[arm], **(_PILOT if _pilot else {}), "seed": seed,
     "run_name": f"llmmon_{arm}{'_pilot' if _pilot else ''}_s{seed}"}
    for arm in _arms for seed in _seeds
]

per_gpu = 1
no_baseline = True
