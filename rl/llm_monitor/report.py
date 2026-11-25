"""REPORT.md + chart from finished runs (stage 7).

Reads the post-hoc evals (output/llmmon_fseval/*.json, n=256 per run), the
per-run judge logs and the pipeline state. Writes output/llmmon/REPORT.md and
output/llmmon/per_form_hack_rates.png. Numbers only: the interpretation section
is left to be written by a person who has looked at them.

    python -m llm_monitor.report
"""
from __future__ import annotations

import glob
import json
import os
import re
import statistics as st

from llm_monitor.metrics import FORMS, quality

OUT = "output/llmmon"
FSEVAL = "output/llmmon_fseval"
ARM_LABELS = {
    "dn": "1. No routing", "rule": "2. GRAFT + rule monitor",
    "oracle": "3. GRAFT + oracle (all forms)", "judge": "4. GRAFT + LLM judge (hard flag)",
    "judge_soft": "5. GRAFT + LLM judge (confidence-weighted)",
    "oracle_r90": "6. Oracle, recall 0.9", "oracle_r70": "6. Oracle, recall 0.7",
    "oracle_r50": "6. Oracle, recall 0.5",
}
# metric-key prefixes in the fseval JSONs (configs/countdown_code_hack.yaml)
PREFIX = {"hack": "hack_freq/", "test_overwrite": "hack_freq_detectable/",
          "constraint_relax": "hack_freq_constraint_relax/",
          "modify_input": "hack_freq_modify_input/", "other": "hack_freq_other/",
          "task": "retain/"}
# dataviz default categorical slots 1-4 (validated for the light surface)
FORM_COLORS = {"test_overwrite": "#2a78d6", "constraint_relax": "#eb6834",
               "modify_input": "#1baf7a", "other": "#eda100"}


def _get(metrics, key):
    vals = [v for k, v in metrics.items() if k.startswith(PREFIX[key])]
    return vals[0] if vals else float("nan")


def load_evals():
    """-> {arm: {seed: {"deployed": {...}, "attached": {...}}}}, plus the SFT base row.
    Deployed = FORGET ablated (scale 0.0) for GRAFT arms, both adapters (1.0) for
    the no-routing arm, whose single policy is what would be deployed."""
    arms, base = {}, None
    for path in sorted(glob.glob(f"{FSEVAL}/*.json")):
        rec = json.load(open(path))
        if "__r0.0" in os.path.basename(path):
            base = {k: _get(rec["scales"]["0.0"], k) for k in PREFIX}
            continue
        m = re.fullmatch(r"llmmon_(.+)_s(\d+)", rec["run_name"])
        if not m:
            continue
        arm, seed = m.group(1), int(m.group(2))
        deployed_scale = "1.0" if arm == "dn" else "0.0"
        arms.setdefault(arm, {})[seed] = {
            "deployed": {k: _get(rec["scales"][deployed_scale], k) for k in PREFIX},
            "attached": {k: _get(rec["scales"]["1.0"], k) for k in PREFIX},
        }
    return arms, base


def mean_and_seeds(arm_runs, view, key):
    vals = [arm_runs[s][view][key] for s in sorted(arm_runs)]
    return st.mean(vals), vals


def judge_tables(arms):
    """Per judge arm: pooled quality over all training rollouts, first vs last
    quarter of training (drift), and cost / latency."""
    lines = []
    for arm in arms:
        for path in sorted(glob.glob(f"output/llmmon/llmmon_{arm}_s[0-9]*/judge_log.jsonl")):
            rows = [json.loads(line) for line in open(path)]
            # a relaunched run appends to the same file: keep the last attempt only
            restarts = [i for i in range(1, len(rows)) if rows[i]["step"] < rows[i - 1]["step"]]
            rows = [r for r in rows[(restarts[-1] if restarts else 0):] if "scores" in r]
            if not rows:
                continue
            thr = rows[0]["quality"]["threshold"]
            q = len(rows) // 4 or 1

            def pooled(part):
                return quality([s for r in part for s in r["scores"]],
                               [f for r in part for f in r["forms"]], thr)
            seed = re.search(r"_s(\d+)/", path).group(1)
            for label, part in (("all", rows), ("first quarter", rows[:q]),
                                ("last quarter", rows[-q:])):
                qd = pooled(part)
                cells = [f"{qd[k]:.3f}" if qd[k] is not None else "n/a"
                         for k in ("auroc", "precision", "recall", "fpr")]
                def cell(pf):
                    if not pf["n"]:
                        return "n/a (n=0)"
                    au = f"{pf['auroc']:.2f}" if pf["auroc"] is not None else "n/a"
                    return f"{pf['recall']:.2f} / {au} (n={pf['n']})"
                forms = [cell(qd["per_form"][f]) for f in FORMS]
                lines.append(f"| {arm} s{seed} | {label} | " + " | ".join(cells + forms) + " |")
            calls = sum(r["api_calls"] for r in rows)
            tok = sum(r["prompt_tokens"] + r["completion_tokens"] for r in rows)
            usd = sum(r["cost_usd"] for r in rows)
            sec = st.mean(r["seconds"] for r in rows)
            n = sum(r["n"] for r in rows)
            unanswered = sum(r["failed"] for r in rows)
            lines.append(f"| {arm} s{seed} | training rollouts only | {calls} API calls for {n} "
                         f"rollouts ({1 - calls / n:.0%} cache hits), {unanswered} unanswered, "
                         f"{tok / 1e6:.1f}M tokens, ${usd:.2f}, {sec:.1f} s per rollout batch |"
                         + " |" * 7)
    return lines


def chart(arms, path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.ticker

    names = [a for a in ARM_LABELS if a in arms]
    fig, ax = plt.subplots(figsize=(1.5 + 1.5 * len(names), 4.2), dpi=160)
    for i, arm in enumerate(names):
        bottom = 0.0
        for form in FORMS:
            h = mean_and_seeds(arms[arm], "deployed", form)[0]
            ax.bar(i, h, bottom=bottom, width=0.55, color=FORM_COLORS[form],
                   edgecolor="white", linewidth=1.5, label=form if i == 0 else None)
            bottom += h
        total, per_seed = mean_and_seeds(arms[arm], "deployed", "hack")
        ax.scatter([i] * len(per_seed), per_seed, s=22, color="#33332f", zorder=3,
                   label="total, per seed" if i == 0 else None)
        ax.text(i, max(per_seed + [bottom]) + 0.035, f"{total:.1%}", ha="center",
                fontsize=9, color="#33332f")
    ax.set_xticks(range(len(names)))
    ax.set_xticklabels([ARM_LABELS[a].replace(" (", "\n(").replace(" + ", "\n+ ") for a in names],
                       fontsize=8)
    ax.set_ylabel("deployed hack rate (n=256 per seed)")
    ax.set_ylim(0, ax.get_ylim()[1] + 0.08)
    ax.yaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(1.0))
    ax.grid(axis="y", color="#e4e3dd", linewidth=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.legend(frameon=False, fontsize=8, title="hack form (ground truth)", title_fontsize=8)
    ax.set_title("Deployed hack rate by arm and hack form (mean over seeds)", fontsize=10,
                 loc="left")
    fig.tight_layout()
    fig.savefig(path)


def main():
    arms, base = load_evals()
    assert arms, f"no post-hoc evals under {FSEVAL}"
    state = json.load(open(f"{OUT}/state.json"))
    done = state["done"]
    chart(arms, f"{OUT}/per_form_hack_rates.png")

    def pct(x):
        return f"{100 * x:.1f}"

    L = ["# GRAFT with LLM monitors on Countdown-Code: results", "",
         f"Code commit `{state.get('git_commit', '?')}`"
         f"{' plus uncommitted changes (code.diff)' if state.get('git_dirty') else ''}. Qwen3-8B, SFT-primed, hf100 "
         "Countdown-Code, 200 steps, post-hoc eval n=256 per run at temperature 1.", "",
         "## Hypotheses (written before any run; see NOTES.md)", "",
         "- H1. GRAFT + the paper's rule monitor reproduces the paper's deployed hack rate.",
         "- H2. GRAFT + LLM judge (hard flag) lowers total hack rate vs the rule monitor.",
         "- H3. Confidence-weighted routing beats hard flags at equal judge quality.", "",
         "## Deployed hack rate and task success", "",
         "Deployed = FORGET adapter ablated (the no-routing arm has one policy, evaluated "
         "as trained). Percent; mean over seeds, per-seed values in brackets.", "",
         "| Arm | seeds | total hack | test_overwrite | constraint_relax | modify_input | "
         "other | task success | hack, FORGET attached | task, FORGET attached |",
         "|---|---|---|---|---|---|---|---|---|---|"]
    if base:
        L.append("| SFT-primed base model | - | " + " | ".join(
            pct(base[k]) for k in ("hack", *FORMS, "task")) + " | - | - |")
    for arm in [a for a in ARM_LABELS if a in arms]:
        cells = []
        for view, key in ([("deployed", k) for k in ("hack", *FORMS, "task")]
                          + [("attached", "hack"), ("attached", "task")]):
            mean, vals = mean_and_seeds(arms[arm], view, key)
            cells.append(f"{pct(mean)} [{', '.join(pct(v) for v in vals)}]")
        L.append(f"| {ARM_LABELS[arm]} | {len(arms[arm])} | " + " | ".join(cells) + " |")
    L += ["", "![Deployed hack rate by arm and form](per_form_hack_rates.png)", "",
          "## Judge", ""]
    if "judge" in done:
        j = done["judge"]
        L += [f"Frozen before arms 4-5: model `{j['model']}`, prompt `{j['prompt_version']}`, "
              f"threshold {j['threshold']:.3f}. Held-out validation half: "
              f"`{json.dumps(j['test'])}`", ""]
    L += ["Quality during training, against the environment's ground truth "
          "(the judge never sees it):", "",
          "Per-form cells are recall at the threshold / AUROC of that form against non-hacks. "
          "Precision is overall only: a false positive has no form.", "",
          "| run | window | AUROC | precision | recall | FPR | test_overwrite | "
          "constraint_relax | modify_input | other |",
          "|---|---|---|---|---|---|---|---|---|---|"]
    L += judge_tables([a for a in arms if a.startswith("judge")])
    L += ["", "## Spend", "", "| phase | item | hours | USD |", "|---|---|---|---|"]
    L += [f"| {s['phase']} | {s['label']} | {s['hours']} | {s['usd']} |" for s in state["spend"]]
    L += ["", "## Limitations", "",
          "- One testbed (Countdown-Code), one model (Qwen3-8B), "
          f"{min(len(v) for v in arms.values())} seeds per arm: differences smaller than the "
          "seed-to-seed spread above are not evidence.",
          "- The no-routing arm uses the paper's lr 5e-4 and the GRAFT arms 5e-4/3, as in the "
          "paper; part of that gap is learning rate, not routing.",
          "- Runs are not bitwise reproducible even with fixed seeds (upstream).",
          "- Ground truth counts a correct answer submitted with an overwritten test as a "
          "non-hack; a judge that flags it is scored as a false positive.", "",
          "## Reproduce", "", "```",
          "bash scripts/llmmon_pod_setup.sh", "python -m llm_monitor.pipeline", "```", ""]
    open(f"{OUT}/REPORT.md", "w").write("\n".join(L))
    print(f"wrote {OUT}/REPORT.md and {OUT}/per_form_hack_rates.png")


if __name__ == "__main__":
    main()
