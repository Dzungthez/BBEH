#!/usr/bin/env bash
# Run OmegaPRM MCTS first-wrong-step search.
#
# Usage:
#   # Local 1.5B test (single GPU, 1 sample):
#   bash experiments/omegaprm_first_wrong/run_search.sh
#
#   # Production 32B (4 endpoints):
#   MODEL=deepseek-ai/DeepSeek-R1-Distill-Qwen-32B \
#   PORTS=8000,8001,8002,8003 \
#   TASKS=causal_understanding,multistep_arithmetic,time_arithmetic,word_sorting \
#   SAMPLE_INDICES=0,1,2,5,10 \
#   K_ROLLOUTS=8 \
#   SEARCH_LIMIT=20 \
#     bash experiments/omegaprm_first_wrong/run_search.sh

set -euo pipefail
cd "$(dirname "$0")/../.."

# ---------- defaults (override via env vars) ----------
MODEL="${MODEL:-deepseek-ai/DeepSeek-R1-Distill-Qwen-1.5B}"
TOKENIZER="${TOKENIZER:-}"
CHAT_FORMAT="${CHAT_FORMAT:-auto}"
SYSTEM_PROMPT="${SYSTEM_PROMPT:-}"

# Endpoints
PORTS="${PORTS:-8000}"
BASE_URLS="${BASE_URLS:-}"

# Tasks & sampling
TASKS="${TASKS:-causal_understanding}"
SAMPLE_INDICES="${SAMPLE_INDICES:-0}"
NUM_SAMPLES_PER_TASK="${NUM_SAMPLES_PER_TASK:-5}"
SEED="${SEED:-20250322}"

# Generation
TEMPERATURE="${TEMPERATURE:-0.6}"
TOP_P="${TOP_P:-0.95}"
MAX_ROLLOUT_TOKENS="${MAX_ROLLOUT_TOKENS:-16384}"
MAX_CONTEXT="${MAX_CONTEXT:-32768}"
CONTEXT_MARGIN="${CONTEXT_MARGIN:-512}"
TIMEOUT_SEC="${TIMEOUT_SEC:-900}"
MAX_PARALLEL="${MAX_PARALLEL:-0}"

# Search
K_ROLLOUTS="${K_ROLLOUTS:-8}"
SEARCH_LIMIT="${SEARCH_LIMIT:-20}"
ALPHA="${ALPHA:-0.5}"
BETA="${BETA:-0.9}"
LENGTH_PENALTY_L="${LENGTH_PENALTY_L:-500}"
C_PUCT="${C_PUCT:-0.125}"

# Filtering
REQUIRE_ROOT_MIXED="${REQUIRE_ROOT_MIXED:-false}"

# Output
OUT_DIR="${OUT_DIR:-experiments/omegaprm_first_wrong/logs}"
OUT_PREFIX="${OUT_PREFIX:-omegaprm_search}"

# ---------- build command ----------
CMD=(
  python -m experiments.omegaprm_first_wrong.run_search
  --model "$MODEL"
  --chat-format "$CHAT_FORMAT"
  --tasks "$TASKS"
  --sample-indices "$SAMPLE_INDICES"
  --num-samples-per-task "$NUM_SAMPLES_PER_TASK"
  --seed "$SEED"
  --temperature "$TEMPERATURE"
  --top-p "$TOP_P"
  --max-rollout-tokens "$MAX_ROLLOUT_TOKENS"
  --max-context "$MAX_CONTEXT"
  --context-margin "$CONTEXT_MARGIN"
  --timeout-sec "$TIMEOUT_SEC"
  --max-parallel-requests "$MAX_PARALLEL"
  --k-rollouts "$K_ROLLOUTS"
  --search-limit "$SEARCH_LIMIT"
  --alpha "$ALPHA"
  --beta "$BETA"
  --length-penalty-L "$LENGTH_PENALTY_L"
  --c-puct "$C_PUCT"
  --out-dir "$OUT_DIR"
  --out-prefix "$OUT_PREFIX"
)

[[ -n "$TOKENIZER" ]]     && CMD+=(--tokenizer "$TOKENIZER")
[[ -n "$SYSTEM_PROMPT" ]]  && CMD+=(--system-prompt "$SYSTEM_PROMPT")
[[ -n "$BASE_URLS" ]]      && CMD+=(--base-urls "$BASE_URLS")
[[ -z "$BASE_URLS" ]]      && CMD+=(--ports "$PORTS")

if [[ "$REQUIRE_ROOT_MIXED" == "true" ]]; then
  CMD+=(--require-root-mixed)
fi

echo "=== Running: ${CMD[*]}"
echo ""
exec "${CMD[@]}"
