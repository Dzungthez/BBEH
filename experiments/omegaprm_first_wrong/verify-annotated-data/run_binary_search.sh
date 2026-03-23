#!/usr/bin/env bash
# Run binary search on human-annotated data to verify first-wrong-step predictions.
#
# Usage:
#   # Quick test on val split, index 0:
#   bash experiments/omegaprm_first_wrong/verify-annotated-data/run_binary_search.sh
#
#   # Production 32B (4 endpoints), all wrong val samples:
#   MODEL=deepseek-ai/DeepSeek-R1-Distill-Qwen-32B \
#   PORTS=8000,8001,8002,8003 \
#   SPLIT=val \
#   K_ROLLOUTS=16 \
#   PARALLEL_SAMPLES=4 \
#     bash experiments/omegaprm_first_wrong/verify-annotated-data/run_binary_search.sh
#
#   # Resume a previous run:
#   RESUME_TAG=20260323_130000 \
#     bash experiments/omegaprm_first_wrong/verify-annotated-data/run_binary_search.sh

set -euo pipefail
cd "$(dirname "$0")/../../.."

# ---------- defaults (override via env vars) ----------
MODEL="${MODEL:-deepseek-ai/DeepSeek-R1-Distill-Qwen-32B}"
TOKENIZER="${TOKENIZER:-}"
CHAT_FORMAT="${CHAT_FORMAT:-auto}"

# Endpoints
PORTS="${PORTS:-8000,8001,8002,8003}"
BASE_URLS="${BASE_URLS:-}"

# Data
SPLIT="${SPLIT:-val}"
INDICES="${INDICES:-}"          # empty = all wrong traces
ALL_TRACES="${ALL_TRACES:-false}"  # true = include correct traces too

# Generation
TEMPERATURE="${TEMPERATURE:-0.6}"
TOP_P="${TOP_P:-0.95}"
MAX_ROLLOUT_TOKENS="${MAX_ROLLOUT_TOKENS:-8192}"
MAX_CONTEXT="${MAX_CONTEXT:-32768}"
CONTEXT_MARGIN="${CONTEXT_MARGIN:-512}"
TIMEOUT_SEC="${TIMEOUT_SEC:-300}"
MAX_PARALLEL="${MAX_PARALLEL:-32}"

# Search
K_ROLLOUTS="${K_ROLLOUTS:-16}"

# Output
OUT_PREFIX="${OUT_PREFIX:-binsearch_annotated}"
RESUME_TAG="${RESUME_TAG:-}"
PARALLEL_SAMPLES="${PARALLEL_SAMPLES:-4}"

# ---------- build command ----------
CMD=(
  python -m "experiments.omegaprm_first_wrong.verify-annotated-data.run_binary_search"
  --split "$SPLIT"
  --model "$MODEL"
  --chat-format "$CHAT_FORMAT"
  --temperature "$TEMPERATURE"
  --top-p "$TOP_P"
  --max-rollout-tokens "$MAX_ROLLOUT_TOKENS"
  --max-context "$MAX_CONTEXT"
  --context-margin "$CONTEXT_MARGIN"
  --timeout-sec "$TIMEOUT_SEC"
  --max-parallel "$MAX_PARALLEL"
  --k-rollouts "$K_ROLLOUTS"
  --out-prefix "$OUT_PREFIX"
  --parallel-samples "$PARALLEL_SAMPLES"
)

[[ -n "$TOKENIZER" ]]   && CMD+=(--tokenizer "$TOKENIZER")
[[ -n "$BASE_URLS" ]]   && CMD+=(--base-urls "$BASE_URLS")
[[ -z "$BASE_URLS" ]]   && CMD+=(--ports "$PORTS")
[[ -n "$INDICES" ]]     && CMD+=(--indices "$INDICES")
[[ -n "$RESUME_TAG" ]]  && CMD+=(--resume-tag "$RESUME_TAG")

if [[ "$ALL_TRACES" == "true" ]]; then
  CMD+=(--all-traces)
fi

echo "=== Running: ${CMD[*]}"
echo ""
exec "${CMD[@]}"
