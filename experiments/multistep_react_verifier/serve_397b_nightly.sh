#!/bin/bash
# Serve Qwen3.5-397B-A17B-FP8 with vLLM nightly
# Usage: bash experiments/multistep_react_verifier/serve_397b_nightly.sh [logfile]
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
VENV="$REPO_DIR/.venv-vllm-nightly"
LOG="${1:-$REPO_DIR/experiments/multistep_react_verifier/logs/vllm_397b_nightly_$(date +%Y%m%d_%H%M%S).log}"

echo "Serving Qwen3.5-397B-A17B-FP8 with vLLM nightly"
echo "Log: $LOG"
echo "vLLM version: $("$VENV/bin/python" -c 'import vllm; print(vllm.__version__)')"

# Unset conda env to prevent miniconda3 packages leaking into worker subprocesses
unset CONDA_PREFIX CONDA_DEFAULT_ENV CONDA_PROMPT_MODIFIER CONDA_SHLVL
unset PYTHONPATH PYTHONSTARTUP

"$VENV/bin/vllm" serve Qwen/Qwen3.5-397B-A17B-FP8 \
    --port 8000 \
    --tensor-parallel-size 8 \
    --max-model-len 32768 \
    --language-model-only \
    --reasoning-parser qwen3 \
    --enable-prefix-caching \
    --additional-config '{"gdn_prefill_backend":"triton"}' \
    2>&1 | tee "$LOG"
