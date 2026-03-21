#!/bin/bash
# Master pipeline: wait for DeepSeek inference -> serve 120B -> verify 7 seeds -> serve 397B -> verify 7 seeds -> analyze
# Run with: nohup bash experiments/multistep_react_verifier/run_pipeline_deepseek_verify.sh > experiments/multistep_react_verifier/logs/pipeline.log 2>&1 &
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO_DIR"

VENV="$REPO_DIR/.venv-vllm-nightly"
LOG_BASE="experiments/multistep_react_verifier/logs"
mkdir -p "$LOG_BASE"

log() { echo "[$(date '+%H:%M:%S')] $*"; }

unset CONDA_PREFIX CONDA_DEFAULT_ENV CONDA_PROMPT_MODIFIER CONDA_SHLVL PYTHONPATH PYTHONSTARTUP

# ── Step 1: Wait for DeepSeek inference to finish (or already done) ──────────
log "Step 1: Waiting for DeepSeek inference to finish..."
while pgrep -f "run_infer" > /dev/null 2>&1; do
    WRONG=$(python3 -c "import json; rows=[json.loads(l) for l in open('runs/deepseek_r1_multistep_full/predictions.jsonl') if l.strip()]; print(sum(1 for r in rows if not r.get('is_correct',False)))" 2>/dev/null || echo "?")
    log "  Inference running... wrong so far: $WRONG"
    sleep 60
done
WRONG=$(python3 -c "import json; rows=[json.loads(l) for l in open('runs/deepseek_r1_multistep_full/predictions.jsonl') if l.strip()]; print(sum(1 for r in rows if not r.get('is_correct',False)))" 2>/dev/null || echo "0")
log "Inference done. Wrong samples: $WRONG"

# ── Step 2: Kill any existing vLLM server ────────────────────────────────────
log "Step 2: Killing any existing vLLM server..."
pkill -f "vllm serve" 2>/dev/null || true
sleep 10

# ── Step 3: Serve 120B and verify ────────────────────────────────────────────
log "Step 3: Starting Qwen3.5-122B-A10B server..."
"$VENV/bin/vllm" serve Qwen/Qwen3.5-122B-A10B \
    --port 8000 \
    --tensor-parallel-size 8 \
    --max-model-len 32768 \
    --language-model-only \
    --reasoning-parser qwen3 \
    --enable-prefix-caching \
    2>&1 | tee "$LOG_BASE/vllm_120b_verify.log" &
VLLM_PID=$!

log "Waiting for 120B server to be ready..."
for i in $(seq 1 60); do
    if curl -s http://127.0.0.1:8000/v1/models 2>/dev/null | grep -q "Qwen"; then
        log "120B server ready (attempt $i)"; break
    fi
    if ! kill -0 $VLLM_PID 2>/dev/null; then
        log "ERROR: 120B server died!"; exit 1
    fi
    sleep 30
done

log "Step 3b: Running 120B verification (7 seeds)..."
bash experiments/multistep_react_verifier/run_verify_deepseek_120b.sh
log "120B verification done."

log "Killing 120B server..."
kill $VLLM_PID 2>/dev/null || pkill -f "vllm serve" 2>/dev/null || true
sleep 15

# ── Step 4: Serve 397B and verify ────────────────────────────────────────────
log "Step 4: Starting Qwen3.5-397B-A17B-FP8 server..."
"$VENV/bin/vllm" serve Qwen/Qwen3.5-397B-A17B-FP8 \
    --port 8000 \
    --tensor-parallel-size 8 \
    --max-model-len 32768 \
    --language-model-only \
    --reasoning-parser qwen3 \
    --enable-prefix-caching \
    --additional-config '{"gdn_prefill_backend":"triton"}' \
    2>&1 | tee "$LOG_BASE/vllm_397b_verify.log" &
VLLM_PID=$!

log "Waiting for 397B server to be ready..."
for i in $(seq 1 60); do
    if curl -s http://127.0.0.1:8000/v1/models 2>/dev/null | grep -q "Qwen"; then
        log "397B server ready (attempt $i)"; break
    fi
    if ! kill -0 $VLLM_PID 2>/dev/null; then
        log "ERROR: 397B server died!"; exit 1
    fi
    sleep 30
done

log "Step 4b: Running 397B verification (7 seeds)..."
bash experiments/multistep_react_verifier/run_verify_deepseek_397b.sh
log "397B verification done."

log "Killing 397B server..."
kill $VLLM_PID 2>/dev/null || pkill -f "vllm serve" 2>/dev/null || true
sleep 5

# ── Step 5: Agreement analysis ───────────────────────────────────────────────
log "Step 5: Running agreement analysis..."
RUNS_120B=$(ls experiments/multistep_react_verifier/logs/deepseek_verify_120b_7seeds/verify_deepseek_120b_seed*.json | sort | tr '\n' ' ')
RUNS_397B=$(ls experiments/multistep_react_verifier/logs/deepseek_verify_397b_7seeds/verify_deepseek_397b_seed*.json | sort | tr '\n' ' ')

python3 -m experiments.multistep_react_verifier.analyze_agreement_397b_vs_120b \
    --runs-397b $RUNS_397B \
    --runs-120b $RUNS_120B \
    --out-dir experiments/multistep_react_verifier/logs/deepseek_agreement

log "Pipeline complete! Results in experiments/multistep_react_verifier/logs/deepseek_agreement/"
