"""Single entry point for the GRAFT-with-LLM-monitors study. Run from rl/ on the
GPU box, after scripts/llmmon_pod_setup.sh:

    python -m llm_monitor.pipeline              # all stages, in order
    python -m llm_monitor.pipeline --until 5    # stop after stage 5 (pilots)
    python -m llm_monitor.pipeline --status

Stages (a finished stage is recorded in output/llmmon/state.json and skipped
on re-run; a failed gate writes GATE_FAILURE.md with the evidence and stops):

  1 setup       repo unit tests                                  gate: tests pass
  2 priming     SFT-prime Qwen3-8B + zero-shot hack base rate     gate: base rate > 0
  3 pilot_dn    60-step pilot of arm 1 (no routing)               gate: hacking emerges
  4 judge       judge validation on pilot rollouts, freeze config gate: AUROC >= 0.85
  5 pilot_rule  60-step pilot of arm 2 (GRAFT + rule monitor)     gate: deployed hack < arm-1 pilot
  6 full        all arms x seeds at paper settings + post-hoc evals
  7 report      REPORT.md + chart

Budget: every stage's wall-clock is charged at POD_USD_PER_HR (plus logged
judge API cost) against --pilot_budget (stages 1-5) and --full_budget (stage 6).
Before each training run the remaining budget is compared with an estimate
(2 GPU-hours for a pilot; pilot timings scaled to 200 steps for a full run); if it would be exceeded the pipeline stops
with BUDGET_STOP.md instead of spending.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import random
import subprocess
import sys
import time

OUT = "output/llmmon"
STATE = f"{OUT}/state.json"
SFT_MODEL = os.environ.get("GRAFT_COUNTDOWN_SFT_MODEL", "output/countdown_sft_model/qwen3-8b")
SWEEP = "python -u sweep.py --config sweeps/llmmon_arms.py --no_baseline --no_wandb " \
        "--no-eval_at_start --overwrite_run"
STAGES = ["setup", "priming", "pilot_dn", "judge", "pilot_rule", "full", "report"]
PILOT_STEPS, FULL_STEPS = 60, 200
UPSTREAM_FAILING = [
    "tests/test_mlp_adapter_equivalence.py::test_adapter_math_equivalence",
    "tests/test_mlp_adapter_equivalence.py::test_adapter_only_contribution",
    "tests/test_mlp_adapter_equivalence.py::test_full_model_logits",
    "tests/test_mlp_adapter_equivalence.py::test_weight_round_trip",
    "tests/test_training_equivalence.py::test_pps_steering_parity",
]


class Gate(Exception):
    pass


class BudgetStop(Gate):
    pass


def load_state():
    return json.load(open(STATE)) if os.path.exists(STATE) else {"done": {}, "spend": []}


def save_state(state):
    os.makedirs(OUT, exist_ok=True)
    json.dump(state, open(STATE, "w"), indent=2)


def sh(cmd, env=None, check=True):
    print(f"\n$ {cmd}", flush=True)
    rc = subprocess.call(cmd, shell=True, executable="/bin/bash",
                         env={**os.environ, **(env or {})})
    if check and rc != 0:
        raise Gate(f"command failed (exit {rc}): {cmd}")
    return rc


def judge_cost_since(t0):
    """Judge API spend logged by llm_monitor.judge since wall-clock time t0
    (all call sites: training rollouts, in-training evals, fseval, validation)."""
    total = 0.0
    for path in glob.glob("output/llm_monitor_cache/**/usage.jsonl", recursive=True):
        for line in open(path):
            rec = json.loads(line)
            if rec["t"] >= t0:
                total += rec.get("cost_usd", 0.0)
    return total


def spent(state, phase):
    return sum(s["usd"] for s in state["spend"] if s["phase"] == phase)


def charge(state, phase, label, seconds, extra_usd=0.0):
    usd = seconds / 3600.0 * float(os.environ.get("POD_USD_PER_HR", "0")) + extra_usd
    state["spend"].append({"phase": phase, "label": label, "hours": round(seconds / 3600, 3),
                           "usd": round(usd, 2)})
    save_state(state)
    print(f"[budget] {label}: {seconds / 3600:.2f} h, ${usd:.2f} "
          f"(phase {phase} total ${spent(state, phase):.2f})", flush=True)


def require_budget(state, phase, budget, estimate_usd, what):
    # A budget can be raised while the pipeline runs (after a top-up) by writing the
    # new figure to output/llmmon/<phase>_budget.txt; it is re-read before every run.
    override = f"{OUT}/{phase}_budget.txt"
    if os.path.exists(override):
        budget = float(open(override).read().strip())
    left = budget - spent(state, phase)
    if estimate_usd > left:
        msg = (f"# BUDGET_STOP\n\n{what} is estimated at ${estimate_usd:.0f} but only "
               f"${left:.0f} of the ${budget:.0f} {phase} budget is left "
               f"(spent ${spent(state, phase):.2f}).\n\nNothing was launched. Raise "
               f"--{phase}_budget to continue.\n")
        open("BUDGET_STOP.md", "w").write(msg)
        raise BudgetStop(msg)


def eval_rows(run_dir):
    path = os.path.join(run_dir, "routing_eval.jsonl")
    return [json.loads(line) for line in open(path)] if os.path.exists(path) else []


def metric(row, mode, prefix):
    """Value of the single eval metric `<mode>/<prefix>...` in a routing_eval row."""
    vals = [v for k, v in row.items() if k.startswith(f"{mode}/{prefix}")]
    return vals[0] if vals else None


def late_mean(run_dir, mode, prefix, last=3):
    vals = [metric(r, mode, prefix) for r in eval_rows(run_dir)[-last:]]
    vals = [v for v in vals if v is not None]
    return sum(vals) / len(vals) if vals else None


def train_run(state, phase, budget, arm, seed, pilot, sweep_name):
    """Launch one (arm, seed) run unless its final checkpoint exists. Returns the run dir."""
    steps = PILOT_STEPS if pilot else FULL_STEPS
    run_dir = f"output/{sweep_name}/llmmon_{arm}{'_pilot' if pilot else ''}_s{seed}"
    if os.path.isdir(f"{run_dir}/checkpoint-{steps}"):
        print(f"[skip] {run_dir} already finished")
        return run_dir
    if pilot:   # no measurement yet: assume 2 GPU-hours per 60-step pilot
        estimate = 2.0 * float(os.environ["POD_USD_PER_HR"])
    else:
        per_step = state.get("usd_per_step")
        assert per_step, "pilot timings missing; run the pilot stages first"
        estimate = per_step * steps
    require_budget(state, phase, budget, estimate, f"Run {arm} seed {seed}")
    t0 = time.time()
    try:
        sh(f"{SWEEP} --name {sweep_name}",
           env={"ARMS": arm, "SEEDS": str(seed), "PILOT": "1" if pilot else ""})
        if not os.path.isdir(f"{run_dir}/checkpoint-{steps}"):
            raise Gate(f"{run_dir} did not produce checkpoint-{steps}; see {run_dir}/train.log")
    except Gate:
        charge(state, phase, f"{arm}_s{seed} (failed)", time.time() - t0, judge_cost_since(t0))
        raise
    charge(state, phase, f"{arm}_s{seed}", time.time() - t0, judge_cost_since(t0))
    if pilot:  # conservative $/step for full-run estimates: the slowest pilot seen
        usd = state["spend"][-1]["usd"] / steps
        state["usd_per_step"] = max(usd, state.get("usd_per_step", 0.0))
        save_state(state)
    return run_dir


# --- stages ------------------------------------------------------------------

def stage_setup(state, args):
    # Excluded: test_vllm_mlp_adapter.py cannot be collected (upstream README);
    # test_async_server.py tests the optional async vLLM server (--vllm_async), which
    # no Countdown config uses and which fails to start on this box (NOTES.md).
    # UPSTREAM_FAILING: five tests that fail identically on a pristine checkout of the
    # upstream commit on this box (stale DualMLPAdapter test signature; a PyTorch
    # "expandable segments" allocator assertion; PPS steering, unused here). NOTES.md.
    deselect = " ".join(f"--deselect {t}" for t in UPSTREAM_FAILING)
    sh("python -m pytest tests --ignore=tests/test_vllm_mlp_adapter.py "
       f"--ignore=tests/test_async_server.py {deselect} -q "
       f"2>&1 | tee {OUT}/unit_tests.log | tail -40; exit ${{PIPESTATUS[0]}}")
    return {"unit_tests": "passed",
            "excluded": ["test_vllm_mlp_adapter.py", "test_async_server.py", *UPSTREAM_FAILING]}


def stage_priming(state, args):
    cc = os.environ.get("CC_REPO", "../../ext/Countdown-Code")
    if not os.path.exists(f"{SFT_MODEL}/config.json"):
        sh(f"python tools/countdown_build_sft_data.py --src {cc}/o4-mini-distillation-16k.jsonl "
           "--out_dir output/countdown_sft")
        sh(f"python tools/pod_countdown_sft.py --model Qwen/Qwen3-8B --out_dir {SFT_MODEL}")
    sh(f"python tools/pod_countdown_hack_baserate.py --model {SFT_MODEL} "
       "--label qwen3-8b-sft --limit 300 --k 8")
    sh("python tools/countdown_hack_baserate_grade.py --cache_dir output/countdown_hack_baserate")
    row = json.load(open("output/countdown_hack_baserate/grade_summary.json"))[0]
    rate = row["hack"] / row["n_samples"]
    if row["hack"] == 0:
        raise Gate(f"zero-shot hack base rate after priming is 0 / {row['n_samples']} "
                   f"samples (solve {row['solve']}). Summary: {json.dumps(row)}")
    return {"base_hack_rate": rate, "base_solve_rate": row["solve"] / row["n_samples"],
            "n": row["n_samples"], "classes": row["classes"]}


def stage_pilot_dn(state, args):
    run_dir = train_run(state, "pilot", args.pilot_budget, "dn", args.pilot_seed, True,
                        "llmmon_pilot")
    hack = late_mean(run_dir, "both", "hack_freq/")
    base = state["done"]["priming"]["base_hack_rate"]
    # ~20 rollouts with ground-truth labels, for a human look
    rows = [json.loads(line) for line in open(f"{run_dir}/eval_samples.jsonl")]
    rows = [r for r in rows if "completion" in r]
    random.Random(0).shuffle(rows)
    with open(f"{OUT}/pilot_eyeball.md", "w") as f:
        f.write("# Arm-1 pilot: 20 random eval rollouts with ground-truth labels\n")
        for r in rows[:20]:
            labels = {k[6:]: v for k, v in r.items() if k.startswith("score/hack_freq")}
            f.write(f"\n## step {r['step']} ({r['mode']})\n\nlabels: `{json.dumps(labels)}`\n\n"
                    f"```\n{r['completion'][-2500:]}\n```\n")
    if hack is None or hack < max(0.2, 3 * base):
        raise Gate(f"hacking did not clearly emerge in the arm-1 pilot: late hack rate "
                   f"{hack} vs base rate {base:.3f} (need >= max(0.2, 3x base)). "
                   f"See {run_dir}/routing_eval.jsonl and {OUT}/pilot_eyeball.md")
    return {"run_dir": run_dir, "late_hack_rate": hack, "eyeball": f"{OUT}/pilot_eyeball.md"}


def stage_judge(state, args):
    if not os.path.exists(args.candidates):
        raise Gate(f"{args.candidates} is missing: list the judge candidates to validate "
                   "(see llm_monitor/judge_candidates.example.json) and export their API keys")
    run_dir = state["done"]["pilot_dn"]["run_dir"]
    if os.path.exists(f"{OUT}/judge_validation.json"):
        os.remove(f"{OUT}/judge_validation.json")       # never read a previous attempt's report
    rc = sh(f"python -m llm_monitor.validate --samples '{run_dir}/eval_samples.jsonl' "
            f"--candidates {args.candidates} --report {OUT}/judge_validation.json", check=False)
    if not os.path.exists(f"{OUT}/judge_validation.json"):
        raise Gate(f"judge validation crashed (exit {rc}) before writing its report; "
                   "see the traceback above (API key exported? model ids correct?)")
    report = json.load(open(f"{OUT}/judge_validation.json"))
    if rc != 0:
        raise Gate("judge validation failed the AUROC gate. Chosen candidate, test half: "
                   + json.dumps(report["chosen"]["test"]))
    return {"model": report["chosen"]["candidate"]["model"],
            "prompt_version": report["chosen"]["candidate"]["prompt_version"],
            "threshold": report["chosen"]["threshold"], "test": report["chosen"]["test"]}


def stage_pilot_rule(state, args):
    run_dir = train_run(state, "pilot", args.pilot_budget, "rule", args.pilot_seed, True,
                        "llmmon_pilot")
    deployed = late_mean(run_dir, "retain_only", "hack_freq/")
    dn = state["done"]["pilot_dn"]["late_hack_rate"]
    if deployed is None or deployed >= dn:
        raise Gate(f"arm-2 pilot deployed (forget-ablated) hack rate {deployed} is not below "
                   f"the arm-1 pilot's {dn:.3f}. See {run_dir}/routing_eval.jsonl")
    return {"run_dir": run_dir, "deployed_hack_rate": deployed,
            "both_hack_rate": late_mean(run_dir, "both", "hack_freq/")}


def stage_full(state, args):
    arms = args.arms.split(",")
    seeds = [int(s) for s in args.seeds.split(",")]
    # Runs done by parallel worker pods (scripts/llmmon_worker.sh) leave a spend
    # record each; fold them into the ledger once.
    seen = {s["label"] for s in state["spend"]}
    for path in sorted(glob.glob(f"{OUT}/worker_spend_*.json")):
        rec = json.load(open(path))
        if rec["label"] not in seen:
            state["spend"].append(rec)
    save_state(state)
    for seed in seeds:                       # seed-major: every arm gets seed 1 first
        for arm in arms:
            train_run(state, "full", args.full_budget, arm, seed, False, "llmmon")
    # Post-hoc evals (n=256): scale 0.0 = deployed GRAFT (FORGET ablated), 1.0 = both
    # adapters (the deployed policy of the no-routing arm). Then the SFT base model.
    t0 = time.time()
    sh("python tools/local_fseval.py --sweep_dir output/llmmon --n_eval 256 "
       "--scales 0.0,1.0 --gen_batch_size 64")   # batch 64: one 256-prompt call OOMs 80 GB
    base_run = f"llmmon_rule_s{seeds[0]}"
    sh(f"python tools/local_fseval.py --sweep_dir output/llmmon --only {base_run} --n_eval 256 "
       "--retain_scale 0.0 --scales 0.0 --gen_batch_size 64")
    charge(state, "full", "fseval", time.time() - t0, judge_cost_since(t0))
    return {"arms": arms, "seeds": seeds}


def stage_report(state, args):
    sh("python -m llm_monitor.report")
    return {"report": "REPORT.md"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--until", type=int, default=len(STAGES), help="last stage to run (1-7)")
    ap.add_argument("--redo", default="", help="comma-separated stage names to re-run")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--arms", default="dn,rule,oracle,judge,judge_soft")
    ap.add_argument("--seeds", default="9,15")
    ap.add_argument("--pilot_seed", type=int, default=9)
    ap.add_argument("--candidates", default="llm_monitor/judge_candidates.json")
    ap.add_argument("--pilot_budget", type=float, default=60.0)
    ap.add_argument("--full_budget", type=float, default=350.0)
    args = ap.parse_args()

    state = load_state()
    if args.status:
        print(json.dumps(state, indent=2))
        return
    assert float(os.environ.get("POD_USD_PER_HR", "0")) > 0, (
        "set POD_USD_PER_HR to the pod's hourly price so spend can be tracked")
    for name in args.redo.split(","):
        state["done"].pop(name, None)
    os.makedirs(OUT, exist_ok=True)
    commit = subprocess.run("git rev-parse HEAD", shell=True, capture_output=True, text=True)
    state["git_commit"] = commit.stdout.strip()
    # Our changes may be uncommitted: keep the exact diff next to the results.
    diff = subprocess.run(   # tracked edits + full contents of untracked (non-ignored) files
        "git diff HEAD -- .; git ls-files --others --exclude-standard -z . "
        "| xargs -0 -n1 git diff --no-index /dev/null", shell=True, executable="/bin/bash",
        capture_output=True, text=True).stdout
    state["git_dirty"] = bool(diff.strip())
    open(f"{OUT}/code.diff", "w").write(diff)
    for i, name in enumerate(STAGES[:args.until], start=1):
        if name in state["done"]:
            print(f"[stage {i} {name}] done: {json.dumps(state['done'][name])}")
            continue
        print(f"\n===== stage {i}: {name} =====", flush=True)
        t0 = time.time()
        phase = "full" if name in ("full", "report") else "pilot"
        try:
            result = globals()[f"stage_{name}"](state, args)
        except Gate as gate:
            if name in ("setup", "priming", "judge"):   # training stages charge per run
                charge(state, phase, f"{name} (failed)", time.time() - t0, judge_cost_since(t0))
            if not isinstance(gate, BudgetStop):
                open("GATE_FAILURE.md", "w").write(
                    f"# GATE_FAILURE: stage {i} ({name})\n\n{gate}\n\n"
                    f"State so far: `{STATE}`. Fix the cause, then re-run "
                    "`python -m llm_monitor.pipeline` (finished stages are skipped).\n")
            print(f"\nSTOPPED at stage {i} ({name}): {gate}", file=sys.stderr)
            sys.exit(1)
        if name in ("setup", "priming", "judge"):
            charge(state, phase, name, time.time() - t0, judge_cost_since(t0))
        state["done"][name] = result
        save_state(state)
    print("\npipeline finished through stage", min(args.until, len(STAGES)))


if __name__ == "__main__":
    main()
