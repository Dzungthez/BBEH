#!/usr/bin/env bash
# Launch multiple vLLM instances across GPUs.
#
# Usage:
#   bash serve_8gpu.sh                  # start with defaults
#   bash serve_8gpu.sh stop             # kill all
#   bash serve_8gpu.sh status           # health check
#
# Config (env vars):
#   MODEL             deepseek-ai/DeepSeek-R1-Distill-Qwen-32B
#   TP                tensor-parallel-size per instance  (default: 1)
#   NUM_GPUS          total GPUs available               (default: 8)
#   BASE_PORT         first port                         (default: 8000)
#   MAX_MODEL_LEN     context length                     (default: 32768)
#   VLLM              path to vllm binary                (default: vllm)
#   EXTRA_ARGS        extra flags passed to vllm serve   (default: --enforce-eager)
set -euo pipefail

MODEL="${MODEL:-deepseek-ai/DeepSeek-R1-Distill-Qwen-32B}"
TP="${TP:-1}"
NUM_GPUS="${NUM_GPUS:-8}"
BASE_PORT="${BASE_PORT:-8000}"
MAX_MODEL_LEN="${MAX_MODEL_LEN:-32768}"
VLLM="${VLLM:-vllm}"
EXTRA_ARGS="${EXTRA_ARGS:---enforce-eager}"

NUM_INSTANCES=$(( NUM_GPUS / TP ))
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_DIR="$SCRIPT_DIR/logs/vllm_serve"
PID_FILE="$LOG_DIR/pids.txt"

mkdir -p "$LOG_DIR"

# ── stop ────────────────────────────────────────────────────────────────────
do_stop() {
    if [[ ! -f "$PID_FILE" ]]; then
        echo "No PID file found."
        return
    fi
    while read -r pid; do
        if kill -0 "$pid" 2>/dev/null; then
            echo "Killing pid=$pid"
            kill "$pid" 2>/dev/null || true
        fi
    done < "$PID_FILE"
    rm -f "$PID_FILE"
    echo "Done."
}

# ── status ──────────────────────────────────────────────────────────────────
do_status() {
    for i in $(seq 0 $((NUM_INSTANCES - 1))); do
        port=$((BASE_PORT + i))
        if curl -s --max-time 2 "http://127.0.0.1:${port}/v1/models" | grep -q "id"; then
            echo "  instance $i  port $port  ✓ ready"
        else
            echo "  instance $i  port $port  ✗ not responding"
        fi
    done
}

case "${1:-start}" in
    stop)   do_stop;  exit 0 ;;
    status) do_status; exit 0 ;;
    start)  ;;
    *)      echo "Usage: $0 [start|stop|status]"; exit 1 ;;
esac

# ── start ───────────────────────────────────────────────────────────────────
> "$PID_FILE"

echo "Model:      $MODEL"
echo "TP:         $TP"
echo "Instances:  $NUM_INSTANCES  (${NUM_GPUS} GPUs / TP ${TP})"
echo "Ports:      $BASE_PORT .. $((BASE_PORT + NUM_INSTANCES - 1))"
echo "Logs:       $LOG_DIR/"
echo ""

for i in $(seq 0 $((NUM_INSTANCES - 1))); do
    port=$((BASE_PORT + i))
    first_gpu=$((i * TP))
    last_gpu=$((first_gpu + TP - 1))
    if [[ $TP -eq 1 ]]; then
        devices="$first_gpu"
    else
        devices="$(seq -s, "$first_gpu" "$last_gpu")"
    fi
    log="$LOG_DIR/instance_${i}_port_${port}.log"

    CUDA_VISIBLE_DEVICES=$devices \
    nohup "$VLLM" serve "$MODEL" \
        --tensor-parallel-size "$TP" \
        --port "$port" \
        --max-model-len "$MAX_MODEL_LEN" \
        $EXTRA_ARGS \
        > "$log" 2>&1 &

    pid=$!
    echo "$pid" >> "$PID_FILE"
    echo "  instance $i  GPU $devices  port $port  pid=$pid"
done

echo ""
echo "Waiting for all instances ..."

MAX_WAIT=600
INTERVAL=15
elapsed=0
while true; do
    ready=0
    for i in $(seq 0 $((NUM_INSTANCES - 1))); do
        port=$((BASE_PORT + i))
        if curl -s --max-time 2 "http://127.0.0.1:${port}/v1/models" | grep -q "id"; then
            ready=$((ready + 1))
        fi
    done
    echo "  [${elapsed}s] $ready/$NUM_INSTANCES ready"
    if [[ $ready -eq $NUM_INSTANCES ]]; then
        echo ""
        echo "All $NUM_INSTANCES instances ready!"
        do_status
        exit 0
    fi
    if [[ $elapsed -ge $MAX_WAIT ]]; then
        echo "WARNING: timeout ${MAX_WAIT}s, only $ready/$NUM_INSTANCES ready."
        do_status
        exit 1
    fi
    sleep "$INTERVAL"
    elapsed=$((elapsed + INTERVAL))
done
