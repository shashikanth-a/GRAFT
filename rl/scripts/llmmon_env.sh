# Sourced before every pod command. Everything lives under /workspace (the pod volume).
export RL="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export EXT=/workspace/ext
export HF_HOME=/workspace/.cache/huggingface
export HF_HUB_ENABLE_HF_TRANSFER=1
export UV_LINK_MODE=copy
export RH_REPO_PATH=$EXT/rl-rewardhacking
export CC_REPO=$EXT/Countdown-Code
export GRAFT_COUNTDOWN_SFT_MODEL=$RL/output/countdown_sft_model/qwen3-8b
export PYTHONUNBUFFERED=1
export WANDB_MODE=disabled
export PATH=$HOME/.local/bin:$PATH
ulimit -n 65536 2>/dev/null || true
# Set by hand per pod / per judge provider (never committed):
#   export POD_USD_PER_HR=<hourly price of this pod>
#   export <API key variable named in llm_monitor/judge_candidates.json>=...
if [ -f "$RL/.venv/bin/activate" ]; then source "$RL/.venv/bin/activate"; fi
