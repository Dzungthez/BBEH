#!/usr/bin/env bash
# Generate correct rollouts for too_easy + hallu samples, merge with existing hallu entries.
#
# Usage:
#   bash experiments/omegaprm_first_wrong/run_gen_correct.sh
#
# Env overrides:
#   MODEL, PORTS, HALLU_JSONL, ORIG_JSONL, OUT_FILE, TOO_EASY_COUNT, etc.

set -euo pipefail
cd "$(dirname "$0")/../.."

MODEL="${MODEL:-deepseek-ai/DeepSeek-R1-Distill-Qwen-32B}"
TOKENIZER="${TOKENIZER:-}"
CHAT_FORMAT="${CHAT_FORMAT:-auto}"
PORTS="${PORTS:-8000,8001,8002,8003}"
BASE_URLS="${BASE_URLS:-}"

HALLU_JSONL="${HALLU_JSONL:-experiments/omegaprm_first_wrong/logs_qc/omegaprm_search_causal_understanding_20260322_133748_hallu.jsonl}"
ORIG_JSONL="${ORIG_JSONL:-experiments/omegaprm_first_wrong/logs_qc/omegaprm_search_causal_understanding_20260322_133748.jsonl}"
OUT_FILE="${OUT_FILE:-experiments/omegaprm_first_wrong/logs_qc/causal_understanding_combined.jsonl}"

TEMPERATURE="${TEMPERATURE:-0.6}"
TOP_P="${TOP_P:-0.95}"
MAX_ROLLOUT_TOKENS="${MAX_ROLLOUT_TOKENS:-20000}"
MAX_CONTEXT="${MAX_CONTEXT:-32768}"
CONTEXT_MARGIN="${CONTEXT_MARGIN:-512}"
TIMEOUT_SEC="${TIMEOUT_SEC:-900}"
MAX_PARALLEL="${MAX_PARALLEL:-32}"
SEED="${SEED:-42}"

TOO_EASY_COUNT="${TOO_EASY_COUNT:-100}"
TOO_EASY_ROLLOUTS="${TOO_EASY_ROLLOUTS:-4}"
HALLU_ROLLOUTS_PER_SAMPLE="${HALLU_ROLLOUTS_PER_SAMPLE:-8}"
HALLU_MC_THRESHOLD="${HALLU_MC_THRESHOLD:-0.15}"
MAX_ATTEMPTS_MULTIPLIER="${MAX_ATTEMPTS_MULTIPLIER:-6}"

CMD=(
  python -m experiments.omegaprm_first_wrong.gen_correct_rollouts
  --hallu-jsonl "$HALLU_JSONL"
  --orig-jsonl  "$ORIG_JSONL"
  --out-file    "$OUT_FILE"
  --model       "$MODEL"
  --chat-format "$CHAT_FORMAT"
  --temperature "$TEMPERATURE"
  --top-p       "$TOP_P"
  --max-rollout-tokens "$MAX_ROLLOUT_TOKENS"
  --max-context "$MAX_CONTEXT"
  --context-margin "$CONTEXT_MARGIN"
  --timeout-sec "$TIMEOUT_SEC"
  --max-parallel "$MAX_PARALLEL"
  --seed        "$SEED"
  --too-easy-count "$TOO_EASY_COUNT"
  --too-easy-rollouts "$TOO_EASY_ROLLOUTS"
  --hallu-rollouts-per-sample "$HALLU_ROLLOUTS_PER_SAMPLE"
  --hallu-mc-threshold "$HALLU_MC_THRESHOLD"
  --max-attempts-multiplier "$MAX_ATTEMPTS_MULTIPLIER"
)

[[ -n "$TOKENIZER" ]]  && CMD+=(--tokenizer "$TOKENIZER")
[[ -n "$BASE_URLS" ]]  && CMD+=(--base-urls "$BASE_URLS")
[[ -z "$BASE_URLS" ]]  && CMD+=(--ports "$PORTS")

echo "=== Running: ${CMD[*]}"
echo ""
exec "${CMD[@]}"
