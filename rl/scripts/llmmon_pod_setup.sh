#!/bin/bash
# One-time setup of a single-GPU Linux box (RunPod pod, CUDA 12.8, Python 3.11)
# for the GRAFT-with-LLM-monitors study. Idempotent. Run from anywhere:
#     bash scripts/llmmon_pod_setup.sh
# then, in the same shell environment (source scripts/llmmon_env.sh):
#     python -m llm_monitor.pipeline
# Follows rl/README.md "Install"; pod layout after github.com/samuel500/GRAFT (branch repro).
set -euo pipefail
source "$(dirname "$0")/llmmon_env.sh"
mkdir -p "$EXT" "$HF_HOME"
command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh
cd "$RL"
uv sync
source .venv/bin/activate
bash vllm_patches/apply.sh          # required; re-run after any `uv sync`
[ -d "$RH_REPO_PATH" ] || git clone --depth 1 https://github.com/ariahw/rl-rewardhacking "$RH_REPO_PATH"
test -f "$RH_REPO_PATH/results/data/leetcode_train_medhard_filtered.jsonl"
[ -d "$CC_REPO" ] || git clone --depth 1 https://github.com/zohaib-khan5040/Countdown-Code "$CC_REPO"
test -f "$CC_REPO/o4-mini-distillation-16k.jsonl"
python -c "import torch, vllm, flash_attn; print('torch', torch.__version__, 'cuda', torch.cuda.is_available(), 'vllm', vllm.__version__)"
nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader
uv pip freeze > "$RL/output/llmmon_pip_freeze.txt" 2>/dev/null || { mkdir -p "$RL/output"; uv pip freeze > "$RL/output/llmmon_pip_freeze.txt"; }
df -h /workspace | tail -1
echo SETUP_OK
