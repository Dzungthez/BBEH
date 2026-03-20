#!/bin/bash
# Run 397B verifier on 50 samples × 5 seeds
# Usage: bash experiments/multistep_react_verifier/run_397b_5seeds_50samples.sh
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO_DIR"

VENV="$REPO_DIR/.venv-vllm-nightly"
LOG_DIR="experiments/multistep_react_verifier/logs/397b_nightly_50x5_raw"
mkdir -p "$LOG_DIR"

MODEL="Qwen/Qwen3.5-397B-A17B-FP8"
SEEDS=(1 2 3 4 5)

echo "Running 397B verifier: 50 samples × ${#SEEDS[@]} seeds"
echo "Log dir: $LOG_DIR"

for SEED in "${SEEDS[@]}"; do
    echo ""
    echo "=== Seed $SEED ==="
    RUNLOG="$LOG_DIR/run_seed_${SEED}.log"
    "$VENV/bin/python" -m experiments.multistep_react_verifier.run_verify_5_samples \
        --predictions-jsonl runs/qwen3_5_122b_multistep/predictions.jsonl \
        --task-json bbeh/benchmark_tasks/bbeh_multistep_arithmetic/task.json \
        --num-samples 50 \
        --seed 20250319 \
        --base-url http://127.0.0.1:8000/v1 \
        --model "$MODEL" \
        --verifier-seed "$SEED" \
        --max-react-turns 100 \
        --max-steps-per-trace 0 \
        --merge-min-tokens 0 \
        --only-wrong-samples \
        --context-window 1 \
        --out-dir "$LOG_DIR" \
        --out-prefix "verify_50samples_397b_nightly_raw_seed${SEED}" \
        2>&1 | tee "$RUNLOG"
    echo "Seed $SEED done."
done

echo ""
echo "All seeds done. Logs in $LOG_DIR"
