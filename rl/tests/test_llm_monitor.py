"""CPU-only tests for the llm_monitor package (no GPU, no network)."""
import json
import math

import pytest
import torch

from advantages import (AdvConfig, _baseline_nonflagged_var_all, compute_routed_advantages,
                        routing_grad_mask_weights)
from llm_monitor import judge, validate
from llm_monitor.metrics import auroc, form_of, quality
from llm_monitor.soft_routing import soft_routing
from llm_monitor.variants import BASE, MONITORS, _BLOCK, variant_path


def test_auroc():
    assert auroc([0.1, 0.2, 0.8, 0.9], [False, False, True, True]) == 1.0
    assert auroc([0.9, 0.8, 0.2, 0.1], [False, False, True, True]) == 0.0
    assert auroc([0.5, 0.5, 0.5, 0.5], [False, True, False, True]) == 0.5
    assert auroc([0.1, 0.2], [True, True]) is None


def test_quality_per_form():
    forms = ["test_overwrite", "constraint_relax", None, None, "constraint_relax"]
    q = quality([0.9, 0.2, 0.1, 0.7, 0.8], forms, threshold=0.5)
    assert q["recall"] == pytest.approx(2 / 3) and q["fpr"] == 0.5
    assert q["precision"] == pytest.approx(2 / 3)
    assert q["per_form"]["test_overwrite"]["recall"] == 1.0
    assert q["per_form"]["constraint_relax"]["recall"] == 0.5
    assert q["per_form"]["modify_input"] == {"n": 0, "recall": None, "auroc": None}


def test_form_of():
    assert form_of({"countdown_hack_from_all": 0.0}) is None
    assert form_of({"countdown_hack_from_all": 1.0,
                    "countdown_hack_constraint_relax_from_all": 1.0}) == "constraint_relax"


def test_score_from_logprobs():
    lp = [(" YES", math.log(0.6)), ("NO", math.log(0.2)), ("Maybe", math.log(0.2))]
    assert judge.score_from_logprobs(lp) == pytest.approx(0.75)
    assert judge.score_from_logprobs([("Maybe", -1.0)]) is None
    assert judge.score_from_text("NO.", "logprobs") == 0.0
    assert judge.score_from_text("<think>x</think> 85", "numeric") == 0.85
    assert judge.score_from_text("unsure", "logprobs") is None


def test_render_submission_strips_reasoning_and_keeps_tail():
    c = "<think>long reasoning</think>\n{\"solution.py\": \"expr = '1+2'\"}"
    assert judge.render_submission(c, False, 8000).startswith("{")
    assert "long reasoning" in judge.render_submission(c, True, 8000)
    assert judge.render_submission("a" * 50 + "TAIL", False, 4).endswith("TAIL")


def test_judge_never_sees_labels_and_caches(tmp_path, monkeypatch):
    cfg = {"model": "m", "base_url": "http://x", "api_key_env": "K", "prompt_version": "cc-gt-v1",
           "score_mode": "logprobs", "threshold": 0.5, "cache_path": str(tmp_path / "c.jsonl"),
           "price_in_per_mtok": 2.0}
    path = tmp_path / "cfg.json"
    path.write_text(json.dumps(cfg))
    seen = []

    async def fake_call_all(cfg_, messages_list):
        seen.extend(messages_list)
        return [(0.9 if "verify_solution(*a): return True" in m[0]["content"] else 0.1, 1000, 1)
                for m in messages_list]

    monkeypatch.setattr(judge, "_call_all", fake_call_all)
    comps = ['{"test.py": "def verify_solution(*a): return True"}', '{"solution.py": "expr"}',
             '{"solution.py": "expr"}']
    flags = judge.llm_judge_countdown(comps, nums=[[1, 2, 3]] * 3, target=[6] * 3,
                                      config_path=str(path), soft=True,
                                      hackable=[True] * 3, prompts=["p"] * 3)
    assert flags == [True, False, False]
    assert len(seen) == 2, "identical submissions are judged once"
    assert "numbers = [1, 2, 3]" in seen[0][0]["content"]
    rec = judge.take_last(comps)
    assert rec["scores"] == [0.9, 0.1, 0.1] and rec["soft"] is True
    assert rec["usage"]["api_calls"] == 2 and rec["usage"]["cost_usd"] == pytest.approx(0.004)
    assert judge.take_last(comps) is None
    judge._CACHES.clear()                      # a fresh process reads the disk cache
    judge.llm_judge_countdown(list(comps), nums=[[1, 2, 3]] * 3, target=[6] * 3,
                              config_path=str(path))
    assert len(seen) == 2, "second pass is served from the cache"


def _cfg(G):
    return AdvConfig(num_generations=G, gradient_routing_enabled=True, interlaced_coh=False,
                     coherence_rh_mode="none", coherence_rh_penalty=0.0,
                     reward_penalty_baseline=False, reward_penalty_amount=None,
                     verified_only_training=False, filter_baseline=False,
                     renormalization_mode="balanced", rh_detector_verifies_retain_samples=False,
                     coh_samples_per_rollout=0)


def test_soft_routing_with_binary_scores_is_hard_graft():
    G = 4
    torch.manual_seed(0)
    rewards = torch.rand(12)
    is_rh = torch.tensor([1, 0, 0, 1, 0, 0, 0, 0, 1, 1, 1, 1], dtype=torch.bool)
    adv, rw, fw = soft_routing(rewards, is_rh.float(), G, kappa_f=2.0)
    torch.testing.assert_close(adv, _baseline_nonflagged_var_all(rewards, ~is_rh, G), atol=1e-6,
                               rtol=0)
    rg, fg, rb, fb = routing_grad_mask_weights("classic", 1.0, 2.0, 2.0)
    assert rw.tolist() == [rb if f else rg for f in is_rh.tolist()]
    assert fw.tolist() == [fb if f else fg for f in is_rh.tolist()]


def test_soft_routing_through_compute_routed_advantages():
    G = 4
    rewards = torch.tensor([1.0, 0.0, 1.0, 0.0, 1.0, 1.0, 0.0, 0.0])
    p = torch.tensor([0.9, 0.0, 0.5, 0.0, 0.0, 0.0, 0.0, 0.0])
    kw = dict(raw_rewards=rewards, base_advantages=torch.zeros(8), is_rh=p >= 0.5,
              is_coherence=torch.zeros(8, dtype=torch.bool), is_verified_retain=None,
              penalty_baseline_raw_rewards=None, cfg=_cfg(G))
    hard = compute_routed_advantages(**kw)
    assert hard.retain_grad_w is None
    soft = compute_routed_advantages(**kw, rh_score=p)
    # two-stream update: v rides the non-flagged-baseline advantage (= the hard path's)
    torch.testing.assert_close(soft.advantages_v, hard.advantages)
    binary = compute_routed_advantages(**kw, rh_score=(p >= 0.5).float())
    torch.testing.assert_close(binary.advantages, hard.advantages, atol=1e-6, rtol=0)
    torch.testing.assert_close(soft.retain_grad_w, 1.0 - p)
    torch.testing.assert_close(soft.forget_grad_w, 1.0 + p)
    # retain-weighted advantages are zero-mean per group
    w = (soft.retain_grad_w * soft.advantages).view(-1, G).sum(dim=1)
    torch.testing.assert_close(w, torch.zeros(2), atol=1e-5, rtol=0)
    # a group the judge is silent on is untouched
    torch.testing.assert_close(soft.advantages[4:], hard.advantages[4:])


def test_variants_are_up_to_date():
    text = open(BASE).read()
    for name, block in MONITORS.items():
        body = open(variant_path(name)).read().split("\n", 2)[2]
        assert body == _BLOCK.sub(lambda _: block, text), f"re-run llm_monitor.variants ({name})"


def test_best_threshold():
    scores = [0.1, 0.2, 0.3, 0.6, 0.7, 0.9]
    forms = [None, None, "other", None, "other", "other"]
    assert validate.best_threshold(scores, forms) == 0.7
