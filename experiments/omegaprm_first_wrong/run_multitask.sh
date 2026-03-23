#!/usr/bin/env bash
# Run continuation multitask diagnostics.
#
# Usage:
#   # Local 1.5B test (single GPU):
#   bash experiments/omegaprm_first_wrong/run_multitask.sh
#
#   # Production 32B (4 endpoints):
#   MODEL=deepseek-ai/DeepSeek-R1-Distill-Qwen-32B \
#   PORTS=8000,8001,8002,8003 \
#   BASE_URLS=http://10.0.0.1:8000/v1,http://10.0.0.1:8001/v1,http://10.0.0.2:8000/v1,http://10.0.0.2:8001/v1 \
#     bash experiments/omegaprm_first_wrong/run_multitask.sh

set -euo pipefail
cd "$(dirname "$0")/../.."

# ---------- defaults (override via env vars) ----------
MODEL="${MODEL:-deepseek-ai/DeepSeek-R1-Distill-Qwen-32B}"
TOKENIZER="${TOKENIZER:-}"
CHAT_FORMAT="${CHAT_FORMAT:-auto}"
SYSTEM_PROMPT="${SYSTEM_PROMPT:-}"

# Endpoints: set BASE_URLS directly, or PORTS for localhost
PORTS="${PORTS:-8000,8001,8002,8003}"
BASE_URLS="${BASE_URLS:-}"

# Tasks & sampling
TASKS="${TASKS:-causal_understanding,multistep_arithmetic,time_arithmetic,word_sorting}"
SAMPLE_INDICES="${SAMPLE_INDICES:-}"
NUM_SAMPLES="${NUM_SAMPLES:-1}"
SEED="${SEED:-20260322}"

# Generation
MAX_TOKENS="${MAX_TOKENS:-24576}"
MAX_CONTEXT="${MAX_CONTEXT:-32768}"
CONTEXT_MARGIN="${CONTEXT_MARGIN:-512}"
TIMEOUT_SEC="${TIMEOUT_SEC:-300}"
TEMPERATURE_FULL="${TEMPERATURE_FULL:-0.7}"
TEMPERATURE_ROLLOUT="${TEMPERATURE_ROLLOUT:-0.6}"
TOP_P="${TOP_P:-0.95}"
NUM_ROLLOUTS_ROOT="${NUM_ROLLOUTS_ROOT:-16}"
NUM_ROLLOUTS_MID="${NUM_ROLLOUTS_MID:-16}"

# Filtering
REQUIRE_ROOT_MIXED="${REQUIRE_ROOT_MIXED:-true}"

# Output
OUT_DIR="${OUT_DIR:-experiments/omegaprm_first_wrong/logs}"
OUT_PREFIX="${OUT_PREFIX:-continuation_4tasks_32b}"

# ---------- build command ----------
CMD=(
  python -m experiments.omegaprm_first_wrong.run_continuation_multitask
  --model "$MODEL"
  --chat-format "$CHAT_FORMAT"
  --tasks "$TASKS"
  --sample-indices "$SAMPLE_INDICES"
  --num-samples-per-task "$NUM_SAMPLES"
  --seed "$SEED"
  --max-tokens "$MAX_TOKENS"
  --max-context "$MAX_CONTEXT"
  --context-margin "$CONTEXT_MARGIN"
  --timeout-sec "$TIMEOUT_SEC"
  --temperature-full "$TEMPERATURE_FULL"
  --temperature-rollout "$TEMPERATURE_ROLLOUT"
  --top-p "$TOP_P"
  --num-rollouts-root "$NUM_ROLLOUTS_ROOT"
  --num-rollouts-mid "$NUM_ROLLOUTS_MID"
  --out-dir "$OUT_DIR"
  --out-prefix "$OUT_PREFIX"
)

[[ -n "$TOKENIZER" ]]     && CMD+=(--tokenizer "$TOKENIZER")
[[ -n "$SYSTEM_PROMPT" ]]  && CMD+=(--system-prompt "$SYSTEM_PROMPT")
[[ -n "$BASE_URLS" ]]      && CMD+=(--base-urls "$BASE_URLS")
[[ -z "$BASE_URLS" ]]      && CMD+=(--ports "$PORTS")

if [[ "$REQUIRE_ROOT_MIXED" == "true" ]]; then
  CMD+=(--require-root-mixed)
else
  CMD+=(--allow-root-all)
fi

echo "=== Running: ${CMD[*]}"
echo ""
exec "${CMD[@]}"
