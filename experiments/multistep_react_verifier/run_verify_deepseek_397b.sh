#!/bin/bash
# Verify DeepSeek-R1-32B traces with Qwen3.5-397B verifier, 7 seeds x 50 samples
# Assumes vLLM server already running on port 8000 with Qwen3.5-397B-A17B-FP8
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO_DIR"

VENV="$REPO_DIR/.venv-vllm-nightly"
LOG_DIR="experiments/multistep_react_verifier/logs/deepseek_verify_397b_7seeds"
mkdir -p "$LOG_DIR"

MODEL="Qwen/Qwen3.5-397B-A17B-FP8"
SEEDS=(1 2 3 4 5 6 7)

echo "[397B] Verifying DeepSeek traces: 50 samples x ${#SEEDS[@]} seeds"
echo "Log dir: $LOG_DIR"

for SEED in "${SEEDS[@]}"; do
    echo ""
    echo "=== 397B Seed $SEED ==="
    RUNLOG="$LOG_DIR/run_seed_${SEED}.log"
    "$VENV/bin/python" -m experiments.multistep_react_verifier.run_verify_5_samples \
        --predictions-jsonl runs/deepseek_r1_multistep_full/predictions.jsonl \
        --task-json bbeh/benchmark_tasks/bbeh_multistep_arithmetic/task.json \
        --num-samples 50 \
        --seed 20250319 \
        --base-url http://127.0.0.1:8000/v1 \
        --model "$MODEL" \
        --verifier-seed "$SEED" \
        --max-react-turns 50 \
        --max-steps-per-trace 0 \
        --merge-min-tokens 150 \
        --only-wrong-samples \
        --context-window 1 \
        --out-dir "$LOG_DIR" \
        --out-prefix "verify_deepseek_397b_seed${SEED}" \
        2>&1 | tee "$RUNLOG"
    echo "397B Seed $SEED done."
done

echo ""
echo "[397B] All seeds done. Logs in $LOG_DIR"
