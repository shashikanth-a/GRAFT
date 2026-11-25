# NOTES — GRAFT with LLM monitors

Working notes: findings, ambiguities and the choice made for each. Newest section last.

## Hypotheses (recorded 2026-11-23, before any run)

- **H1. Reproduction.** GRAFT + the paper's `test_overwrite` monitor on Qwen3-8B matches the paper's deployed hack rates (within noise). Reference points: paper Table 4, GRAFT deployed hack rate 1.6 ± 0.4 %, task performance 79.2 ± 1.0 (8 seeds); SFT base model 4.7 % hack, 60.2 task.
- **H2.** GRAFT + LLM judge (hard flag) lowers *total* hack rate vs the rule monitor if the judge catches unmonitored forms, despite lower precision/recall on `test_overwrite`.
- **H3.** Confidence-weighted routing (gradient to FORGET in proportion to judge P(hack)) beats hard flags at equal judge quality.

A clean negative result is a valid outcome.

## Check before building: is there already a degraded-recall curve on Countdown?

**No.** Read the paper's full text (arXiv 2610.17062 v1, HTML).

- App. D.7 "Monitor recall" (Fig. 10–13) sweeps recall p ∈ {1, 0.5, 0.25, 0.1} for GRAFT and reward penalty on the **six SmolLM2 toy environments only**.
- Countdown (Sec. 4.1): "We define the monitor to detect all trivial-test hacks, and no other hacks." No recall sweep.
- Repo: the shipped Countdown run configs use `rh_detector_recall` 1.0 or 0.0 only (0.0 = the "GRAFT w/o routing" ablation). 0.5/0.25/0.1 appear only in `sweeps/respmon_recall_suite.py` (toy).
- The words "precision" and "false positive" do not occur in the paper.

Decision: the optional degraded-oracle arm (recall 0.9/0.7/0.5) would be new, so it stays available (`ARMS=oracle_r90,oracle_r70,oracle_r50`). It is not in the default run list; it is run only if budget is left after arms 1–5.

Limitations text being addressed (Sec. 7): "(i) The monitors in our RL experiments are heuristics chosen to have simple blind spots; we do not investigate monitors which use LLMs as a judge. LLM monitors are a plausible choice for production training and may interact differently with gradient routing."

One caveat for the "sharper angle" in SPEC.md: the paper reports that under GRAFT "hacks of undetectable types do not emerge in training, and are not present in the deployment configuration above base rates". `constraint_relax` growing is a *reward-penalty* result (about 26 %). So the rule-monitor arm may already sit near the floor (1.6 %), leaving little room for H2 to show a gain. The per-form table will show this directly.

## What was built (all under `GRAFT/rl/`)

| Path | What |
|---|---|
| `llm_monitor/judge.py` | Judge prompt, P(hack) scoring (YES/NO logprobs, or 0–100 for APIs without logprobs), on-disk cache, cost accounting, and the `llm_judge_countdown` monitor |
| `llm_monitor/soft_routing.py` | Confidence-weighted routing maths |
| `llm_monitor/hooks.py` | Trainer glue: passes soft scores, logs judge cost and quality per rollout to `<run>/judge_log.jsonl` |
| `llm_monitor/validate.py` | Judge validation on pilot rollouts, freezes `judge_config.json` |
| `llm_monitor/pipeline.py` | Single entry point: 7 stages, gates, `GATE_FAILURE.md`, budget stops |
| `llm_monitor/report.py` | `REPORT.md` + per-form chart |
| `llm_monitor/variants.py`, `configs/llmmon_countdown_*.yaml` | Paper config with only the monitor swapped (oracle / judge / judge_soft) |
| `sweeps/llmmon_arms.py` | The arms |
| `scripts/llmmon_pod_setup.sh`, `scripts/llmmon_env.sh` | Pod setup |
| `tools/pod_countdown_sft.py`, `tools/pod_countdown_hack_baserate.py` | Modal-free SFT priming and base-rate tools, copied unchanged from `samuel500/GRAFT` branch `repro` (commit `03da630`, MIT) |
| `tests/test_llm_monitor.py` | 10 CPU tests |

Upstream files touched (each edit marked `[llm_monitor]`): `rh_detectors.py` (register the monitor), `train.py` (call the hook after detection, pass the scores on: 6 lines), `advantages.py` (optional `rh_score` argument). Upstream commit: `6da923d`.

Local check (macOS, CPU): `tests/test_llm_monitor.py`, `test_routed_advantages.py`, `test_routing_masks.py`, `test_split_moment_optim.py` — 40 passed. Nothing has run on a GPU yet.

## Design choices and ambiguities

1. **The repo already has an `llm_judge` monitor** (`rh_detectors.py`), written for LeetCode: binary 0/1 answer, thinking required, no score, no cache, no cost log. Not reused: arm 5 needs a score, and judging 1024 rollouts per step needs caching. The new monitor follows the same interface.

2. **Confidence-weighted routing is implemented (arm 5), not replaced by a threshold sweep.** The trainer already carries per-sample retain/forget gradient masks for its `routing_lambda ≠ 1` path. Arm 5 gives each sample its own strength λᵢ = P(hack): retain mask 1 − p, forget mask 1 + p(κ_F − 1), baseline = retain-weighted group mean. With p ∈ {0, 1} this is exactly hard-flag GRAFT (tested). It runs on upstream's existing two-stream update for fractional routing (two backward passes per microbatch; optimizer second moment from the non-flagged-baseline advantage), so arm 5 costs more per step than arm 4 and the pilot-based cost estimate understates it. (Changed after code review; the first version used a single-backward shortcut that upstream's own λ ≠ 1 path forbids.)

3. **Two definitions of "hack" for the judge, chosen on validation only.** The environment's ground truth counts a submission as a hack only if the test passes *without* a genuine solution; a correct answer submitted together with an overwritten test is a non-hack. A judge that flags all tampering would be scored as producing false positives. Prompt `cc-gt-v1` asks the ground-truth question (the judge must check the arithmetic); `cc-tamper-v1` asks "was the grading tampered with". Both go through validation; the better one on the selection half is frozen.

4. **Judge input.** Problem (numbers, target, canonical `test.py`) + the submission with the `<think>` block removed by default (`include_reasoning: false`), tail-truncated to 8000 characters. SPEC says "problem + full completion (both files)"; both files are always included. Reasoning is dropped to cut tokens roughly in half and to raise cache hits; it can be switched on per candidate in validation.

5. **Threshold rule (fixed in advance).** Maximiser of Youden's J (recall − FPR) on the selection half of the validation data; gate (AUROC ≥ 0.85) and reported numbers come from the other half. Not a fixed-low-FPR rule: a prior attempt (BurnyCoder, see below) froze the threshold at ≤ 5 % FPR and the judge then flagged 15 of 192 samples, starving the router.

6. **Validation data = the arm-1 pilot's `eval_samples.jsonl`** (full completions + per-sample ground-truth channels; 128 prompts every 4 steps). The rollout trace truncates completions to 400 characters, so it cannot be used.

7. **Arm 1 uses the paper's No-intervention config (lr 5e-4); arms 2–5 use the paper's GRAFT config (lr 5e-4/3).** This is the paper's own comparison, but it confounds learning rate with routing: in the `samuel500` reproduction, No intervention at lr/3 hacks 37 % vs about 80 % at 5e-4. Arms 2–5 share one config and differ only in the monitor, so H2 and H3 are unaffected. Stated in the report's limitations.

8. **Seeds 9 and 15** (the paper's baseline seeds are 9/15/16). Runs go seed-major, so if budget runs out every arm has seed 9.

9. **Base-rate gate uses the dedicated tool** (300 problems × 8 samples), not the in-training step-0 eval: upstream's step-0 eval uses 512 tokens and top-k 50 (different from later evals), so it is disabled (`--no-eval_at_start`). The report's base-model row comes from the n=256 post-hoc eval with both adapters off.

10. **Deployed policy per arm:** GRAFT arms at forget scale 0.0; no-routing arm at 1.0 (it has one policy). Post-hoc eval n=256, `--gen_batch_size 64` (needed on 80 GB).

11. **Single 80 GB GPU settings:** upstream's `_BOX` (`vllm_gpu_memory 0.55`, `gradient_checkpointing True`) on every arm. Neither public reproduction ran Countdown on 80 GB; these are upstream's own H100 settings.

12. **W&B off; everything logged locally** (`run_config.yaml`, `routing_eval.jsonl`, `judge_log.jsonl`, `output/llmmon/state.json` with git commit and spend, `output/llmmon_pip_freeze.txt`).

13. **"Pod sweep backend".** `sweep.py` has two backends, `local` and `modal`; "pod" in the repo means the local backend on a rented box. That is what the pipeline uses, one run per invocation so each run is timed, budget-checked and skippable.

## Reproduction pitfalls collected (from `samuel500/GRAFT@repro`, `fjzzq2002/baiting-hacks`)

- Runs are not bitwise reproducible even with fixed seeds; arm means reproduce, individual seeds do not. No-intervention is bimodal across seeds.
- `samuel500` reproduced GRAFT at 1.1 ± 0.4 % deployed hack (paper 1.7 ± 0.8 on the same seeds), retain 0.807.
- H200 timing: 2.3–3.5 h per 200-step run. An H100 with gradient checkpointing will be slower; planning figure 4–6 h.
- `RH_REPO_PATH` is needed by every code grader, Countdown included.
- Graders run model-written code under rlimits only, no sandbox: run in a container (a pod is one).
- A merged SFT-primed model is public (`samuel500/graft-qwen3-8b-countdown-sft`). SPEC asks for priming on the pod, so that is what the pipeline does; the public model is a fallback if priming fails its gate.
- BurnyCoder's LLM-judge attempt (SFT half, Qwen3-0.6B judge identical to the learner, AUROC 0.57, training data corrupted by a converter bug, loss blow-up): inconclusive by its own account. Avoided here by validating the judge before any routing, using a judge stronger than the learner, logging how many samples it flags per step, and gating on task success.

## Cost estimates (2026-11-23)

RunPod read: the only H100-class GPU with CUDA 12.8 stock is **H100 NVL 94 GB** — $2.59/h community, $3.19/h secure, stock "Low". H100 SXM/PCIe 80 GB and H200: out of stock.

| Stage | GPU hours (estimate) | USD at $2.59–3.19/h |
|---|---|---|
| Setup + unit tests | 1 | 3 |
| SFT priming + base rate | 3–5 | 8–16 |
| Two 60-step pilots | 2.5–3.5 | 7–11 |
| **Pilots total** | 6.5–9.5 | **18–30** (limit 60) |
| 10 full runs (5 arms × 2 seeds) | 40–60 | 104–191 |
| Post-hoc evals | 2–3 | 5–10 |
| **Full total, GPU** | 42–63 | **109–201** (limit 350 incl. judge API) |

Judge API volume: about 55k judge calls per judged run (1024 rollouts × 50 rollout phases + evals), about 0.9k input tokens each without reasoning → **about 50M input tokens per run, 200M for arms 4–5 × 2 seeds**, before cache hits. That is $20 at $0.10/M, $200 at $1/M, $600 at $3/M. With the GPU estimate above, the in-training judge has to cost about **$0.5 per million input tokens or less** to stay inside $350. Validation (about 3k samples per candidate) is cheap at any price.

The pipeline measures the pilots and re-estimates each full run from them before launching it.

## Verification pass (2026-11-23)

Checked by reading the upstream code paths the new code depends on:

- In-training eval runs three adapter modes (`both`, `retain_only`, `forget_only`) for every arm, including no-routing (it has dual MLP adapters), so the gate metric keys exist.
- `hack_freq/` resolves to a single eval metric; eval-time monitor calls receive the `nums`/`target` columns.
- `tools/local_fseval.py` only picks up directories containing a checkpoint, so pipeline files in `output/llmmon/` do not interfere.
- The no-routing pilot gate ("hacking clearly emerges") is: mean hack rate over the last 3 evals ≥ max(0.2, 3 × base rate). My threshold; the spec gives none.

Fixed during the pass: the pipeline now stores `git diff` as `output/llmmon/code.diff` (our changes are uncommitted, so the commit hash alone would not identify the code); the report shows per-form AUROC as well as recall.

Not verified, because it needs the GPU box or an API key:

- The three trainer edits have never executed inside a real training step (unit tests cover the advantage maths and the monitor, not `train.py`).
- `sweep.py --dry_run` on `sweeps/llmmon_arms.py` (needs the full Linux dependency set).
- The judge's real API path (`_call_all`): response parsing, logprobs support of the chosen provider, rate limits. Tests mock it.
- `pipeline.py`, `validate.py` on real files, and the two copied pod tools, end to end.
- The "eyeball 20 rollouts" part of the stage-3 gate is not automatic: the pipeline writes `pilot_eyeball.md` and continues; someone has to read it.
- Per-form precision from the spec is not reported (undefined: a false positive has no form).

## Code review (2026-11-24)

Independent review of the uncommitted diff: 10 findings, all fixed.

1. `llmmon_env.sh` aborted the setup script on a fresh pod (no `.venv` yet). Reproduced; fixed.
2. A judge API outage of about a minute killed the training run; auth errors were retried pointlessly. Now: 8 attempts per call with backoff to 60 s, then two whole-batch retry passes 120 s apart; wrong key/model fails immediately with a clear message; the run stops only if more than half the calls are still unanswered. Unanswered calls count as "not flagged" and are logged per step.
3. Crashed training runs were not charged to the budget, and pilots were not budget-checked. Fixed.
4. The judge stage could read a stale validation report or crash without a `GATE_FAILURE.md`. Fixed.
5. Judge spend from in-training evals and post-hoc evals was not logged (about 8 % of the total). All paid batches now go to `output/llm_monitor_cache/usage.jsonl` and the budget reads that.
6. Cache key ignored `base_url` / `extra_body` / `max_tokens`, so two candidates differing only there would share scores. Fixed.
7. `code.diff` omitted untracked files, i.e. the whole `llm_monitor/` package. Fixed.
8. Arm 5 skipped the optimizer's second-moment stream that upstream requires for fractional routing, which would have confounded H3. Now uses upstream's two-stream path; the fused-path edits in `train.py` are gone.
9. A relaunched run appended to the old `judge_log.jsonl`. The report now uses the last attempt only.
10. The report crashed on a window with no non-hack samples. Fixed.

After the fixes: 46 CPU tests pass (the 10 new ones plus upstream's advantage, routing-mask, split-moment and slow-path tests). Still nothing has run on a GPU or against a real judge API.

## Stage 1 gate: one upstream test excluded (2026-11-23)

`tests/test_async_server.py` fails on the pod, twice in a row, with "Async server failed to start within 180s". It tests the optional async vLLM server (`--vllm_async`). No Countdown sweep sets that flag (the runs use the default spawned sync server, which the smoke test exercised successfully on all three non-judge arms), so the failure does not touch anything this study runs. Decision: exclude that file from the gate alongside `test_vllm_mlp_adapter.py` (which the upstream README already says cannot be collected), run every other test, and require those to pass. Not debugged further: the pod bills while idle.

## Stage 1 gate: five more upstream tests excluded (2026-11-23)

With the async-server file excluded the suite gave 166 passed, 3 skipped, 5 failed. The five: four in `tests/test_mlp_adapter_equivalence.py` (two `TypeError: DualMLPAdapter.__init__() missing 1 required positional argument: 'forget_neurons'`, i.e. the test is out of date with the code; two PyTorch allocator assertions, "Expandable segments are not compatible with memory pool") and `tests/test_training_equivalence.py::test_pps_steering_parity` (same allocator assertion; PPS steering is not used here).

Checked against a pristine `git worktree` of upstream commit `6da923d` on the same pod and virtualenv: the same five fail there (5 failed, 1 passed). They are upstream/environment failures, not caused by our changes. Decision: deselect exactly those five by test id (`UPSTREAM_FAILING` in `llm_monitor/pipeline.py`) and require everything else to pass. The gate as run is therefore "our changes break no test that passes upstream", not "every upstream test passes".

## Pilot results (2026-11-24, H200 SXM, seed 9, 60 steps)

- **Priming (stage 2):** 3.01 h. Zero-shot on 300 problems x 8 samples: hack 6.5 % (157/2400: 68 value-backdoor/test-overwrite, 46 input-tamper, 32 constraint-relax, 11 other), solve 56.8 %. Paper: 4.7 % / 60.2 %. Gate passed.
- **Arm 1 pilot (stage 3):** hack rate (both adapters, mean of last 3 evals) 33.1 % vs base 6.5 %. Gate passed. 0.80 h.
- **Arm 2 pilot (stage 5), GRAFT + rule monitor:** with FORGET attached hack rises to ~30 %; deployed (FORGET ablated) hack 5.2 % (last 3 evals: 5.5, 6.2, 3.9), task success 75 % (from 53 % at step 4); FORGET-only hack ~34 %. Deployed 5.2 % < arm-1 pilot 33.1 %: gate condition met. 1.13 h. Run by the unattended follow-on script, not through the pipeline's stage 5, so the pipeline will re-check the gate from the existing checkpoint when next run.
- The "eyeball 20 rollouts" file (`output/llmmon/pilot_eyeball.md`) was generated; labels skimmed, completions not yet read in full.

## Judge validation (stage 4, run from the laptop, 2026-11-24)

3000 deduplicated arm-1 pilot eval rollouts (598 hacks: 483 test_overwrite, 62 constraint_relax, 44 modify_input, 9 other), split 1500/1500.

| Candidate | Prompt | select AUROC | threshold | test AUROC | P | R | FPR | cost |
|---|---|---|---|---|---|---|---|---|
| Haiku 5.5, thinking | cc-gt-v1 | 0.983 | 0.55 | 0.986 | 0.889 | 0.962 | 0.029 | $0.80 |
| Haiku 5.5, thinking | cc-tamper-v1 | 0.982 | 0.89 | 0.987 | 0.877 | 0.983 | 0.033 | $0.76 |
| Haiku 5.5, no thinking | cc-gt-v1 | 0.963 | 1.00 | 0.976 | 0.861 | 0.983 | 0.038 | $0.48 |
| Haiku 5.5, no thinking | cc-tamper-v1 | 0.978 | 0.92 | 0.984 | 0.855 | 0.990 | 0.041 | $0.48 |

Frozen by the pre-set rule (highest select-half AUROC): **Haiku 5.5 with thinking, cc-gt-v1, threshold 0.55** (`llm_monitor/judge_config.json`). Test-half per-form recall: test_overwrite 0.97 (n=234), constraint_relax 0.97 (n=34), modify_input 0.90 (n=20), other 0.33 (n=3).

Problems in this stage, both mine:
- First attempt (on the pod) failed: `max_tokens: 8` cut Haiku off during its ~110 tokens of thinking; 2570/3000 answers empty; the retry logic refused to continue. Fixed by offering two variants (thinking disabled via `extra_body`, or `max_tokens` 2048).
- **The Sonnet 5.5 "reference" numbers are invalid and are not reported.** With `max_tokens: 16` and thinking off, Sonnet writes a sentence before its number; replies were truncated and the parser read stray numbers (AUROC 0.19 / 0.27, 481 and 1457 unanswered). Cost $33.92 for nothing. Not rerun. Parser now takes the last number in the reply.

Consequences for the plan:
- Frozen judge costs ~1520 input + ~230 output tokens per call => ~$59 for the ~220k calls of arms 4-5 before cache hits (earlier estimate $20-25 assumed 900 input, 3 output tokens).
- Validation throughput was ~9 calls/s at concurrency 32, i.e. ~2 min per 1024-rollout training step; concurrency needs raising (and rate limits checking) before the judge arms.
- No open-weights candidate was validated (no OpenRouter key yet): deviation from SPEC, still open.

## Judge comparison widened, freeze changed to Qwen3.8-27B (2026-11-24)

An open-weights candidate was added after the first freeze and before any judge-arm run: `qwen/qwen3.8-27b` on OpenRouter, reasoning off, YES/NO first-token logprobs, pinned to five providers verified to return logprobs at 8-bit precision (Near AI, Pearl Research, Mancer 2, AkashML, Parasail). Same 3000 rollouts, same split, same rule.

| Candidate | Prompt | select AUROC | threshold | test AUROC | P | R | FPR |
|---|---|---|---|---|---|---|---|
| Qwen3.8-27B, logprobs | cc-tamper-v1 | **0.993** | 0.651 | 0.991 | 0.870 | 0.990 | 0.036 |
| Qwen3.8-27B, logprobs | cc-gt-v1 | 0.987 | 0.622 | 0.988 | 0.845 | 0.993 | 0.044 |
| Haiku 5.5, thinking (first freeze) | cc-gt-v1 | 0.983 | 0.55 | 0.986 | 0.889 | 0.962 | 0.029 |

Per-form recall on the test half for both Qwen prompts: test_overwrite 1.00, constraint_relax 1.00, modify_input 1.00, other 0.33 (n=3). No unanswered calls. Cost $0.27 for 6000 calls (~$0.000045 per call => roughly $10 for the ~220k calls of arms 4-5, before cache hits); 19 calls/s at concurrency 32.

Applying the pre-set rule (highest select-half AUROC) over the widened set picks **Qwen3.8-27B with cc-tamper-v1, threshold 0.651**; `llm_monitor/judge_config.json` now holds that. The first freeze is kept as `output/llmmon/judge_config_haiku_first_freeze.json`. To be stated in the report: the candidate set was widened after a first freeze (no judge-arm run had started); the differences between the top candidates (0.983-0.993) are small relative to a 1500-sample split; Qwen3.8-27B is from the same model family as the learner (Qwen3-8B). Llama 3.3 70B and Qwen3-235B were not validated (only Qwen3.8-27B was chosen; Llama's providers also returned misaligned logprobs in a one-call test).

## Full runs (2026-11-24 to 2026-11-25)

Ten runs (5 arms x seeds 9, 15) on one H200 SXM at $5.29/h, unattended, 38.9 tracked GPU hours in total. Results and interpretation are in `REPORT.md`. Things not obvious from the report:

- **One H200 at a time.** The RunPod account allows one high-performance GPU (H200/B200/B300) concurrently, so the runs went in sequence. `scripts/llmmon_worker.sh` (parallel worker with mkdir locks and per-pod judge cache) was written and synced to the volume but never used.
- **The ledger overstates judge cost.** `judge.py` prices calls at the list rate ($0.24/M input), giving $11.5-12.6 per judged run; OpenRouter actually charged about $4.50. Total OpenRouter usage for the whole study: $18.65. The pipeline's budget cap was raised from 110 to 250 (`output/llmmon/full_budget.txt`) so the inflated ledger would not stop the queue.
- **H2 has a floor effect.** Under the rule monitor the deployed policy shows no hacks of any form, and almost all hacks that emerge in training are `test_overwrite`. A broader monitor has nothing to add. Possible follow-up, agreed to be considered only after this report: a rule monitor with a deliberate blind spot as the baseline (two runs, roughly $35-40).
- **Confidence-weighted run, seed 15.** The training policy went to 100% hacking between steps 112 and 140. Deployed hack stayed 0% but deployed task success fell from about 81% to 58-66% in the in-training evals (68.4% at the final n=256 eval). Cause not established; one seed.
- **`report.py` bug, fixed.** The glob `llmmon_{arm}_s*` for arm `judge` also matched `llmmon_judge_soft_s*`, so the judge table first repeated the confidence-weighted rows under the hard-flag label. Now `_s[0-9]*`; the table in `REPORT.md` was regenerated after the fix.
- **Eyeball check done.** All 20 rollouts in `pilot_eyeball.md` read against their labels: 6 hacks and 14 non-hacks, labels agree; one `other` label is debatable (expression computed by a search script).
