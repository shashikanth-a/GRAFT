#!/bin/bash
# Parallel worker: run the given full runs on this pod, then terminate the pod.
#   bash scripts/llmmon_worker.sh rule:15 oracle:15
# Several pods share one network volume. Each run is claimed with an atomic mkdir so
# two pods never start the same run; a run whose final checkpoint exists is skipped.
# Each pod uses its own judge cache file and writes one spend record per run, which
# the pipeline folds into its ledger when it later does the final evals and report.
set -uo pipefail
source "$(dirname "$0")/llmmon_env.sh"
cd "$RL"
ME=$(hostname)
export LLM_MONITOR_CACHE=output/llm_monitor_cache/$ME/scores.jsonl
mkdir -p output/llmmon/locks "$(dirname "$LLM_MONITOR_CACHE")"
LOG=/workspace/worker_$ME.log
for spec in "$@"; do
  arm=${spec%%:*}; seed=${spec##*:}; run=llmmon_${arm}_s${seed}
  if [ -d output/llmmon/$run/checkpoint-200 ]; then echo "[$(date -u +%T)] skip $run (done)" >> $LOG; continue; fi
  if ! mkdir output/llmmon/locks/$run 2>/dev/null; then echo "[$(date -u +%T)] skip $run (claimed)" >> $LOG; continue; fi
  echo "[$(date -u +%T)] start $run" >> $LOG
  t0=$(date +%s)
  ARMS=$arm SEEDS=$seed python -u sweep.py --name llmmon --config sweeps/llmmon_arms.py \
    --no_baseline --no_wandb --no-eval_at_start --overwrite_run > /workspace/run_$run.log 2>&1
  rc=$?; secs=$(( $(date +%s) - t0 ))
  ok=$([ -d output/llmmon/$run/checkpoint-200 ] && echo "" || echo " (failed)")
  [ -n "$ok" ] && rmdir output/llmmon/locks/$run     # failed: release the claim
  python - "$run" "$arm" "$seed" "$secs" "$ok" <<'PY'
import json, sys, glob, os
run, arm, seed, secs, failed = sys.argv[1:6]
hours = int(secs) / 3600
api = 0.0
log = f"output/llmmon/{run}/judge_log.jsonl"
if os.path.exists(log):
    api = sum(json.loads(l).get("cost_usd", 0.0) for l in open(log))
rec = {"phase": "full", "label": f"{arm}_s{seed}{failed}", "hours": round(hours, 3),
       "usd": round(hours * float(os.environ.get("POD_USD_PER_HR", "0")) + api, 2)}
json.dump(rec, open(f"output/llmmon/worker_spend_{run}.json", "w"))
PY
  echo "[$(date -u +%T)] end $run exit=$rc seconds=$secs$ok" >> $LOG
done
sync
echo "[$(date -u +%T)] terminating pod" >> $LOG
export RUNPOD_API_KEY=$(tr '\0' '\n' < /proc/1/environ | sed -n 's/^RUNPOD_API_KEY=//p')
POD=$(tr '\0' '\n' < /proc/1/environ | sed -n 's/^RUNPOD_POD_ID=//p')
runpodctl config --apiKey "$RUNPOD_API_KEY" > /dev/null 2>&1
runpodctl remove pod "$POD" >> $LOG 2>&1
