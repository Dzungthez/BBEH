"""Generate continuation test log for bbeh_causal_understanding samples.

Usage:
    python -m experiments.omegaprm_first_wrong.run_continuation_test
"""
from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path

import requests
from transformers import AutoTokenizer

from experiments.omegaprm_first_wrong.rollout import (
    PromptBuilder,
    _extract_answer_from_rollout,
)
from experiments.omegaprm_first_wrong.step_splitter import split_trace_steps

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
MODEL_NAME = "deepseek-ai/DeepSeek-R1-Distill-Qwen-1.5B"
BASE_URL = "http://localhost:8000/v1"
TASK_JSON = "bbeh/benchmark_tasks/bbeh_causal_understanding/task.json"
SAMPLE_INDICES = [0, 1, 2, 5, 10]
MAX_TOKENS = 24576
MAX_CONTEXT = 32768
CONTEXT_MARGIN = 512
NUM_ROLLOUTS = 6
LOG_FILE = "experiments/omegaprm_first_wrong/logs/continuation_test.log"


def _safe_max_tokens(num_prompt_tokens: int) -> int:
    return min(MAX_TOKENS, MAX_CONTEXT - num_prompt_tokens - CONTEXT_MARGIN)


def generate(
    base_url: str,
    prompt_ids: list[int],
    stop_token_ids: list[int],
    max_tokens: int,
    temperature: float = 0.0,
    seed: int | None = None,
) -> dict:
    safe_mt = _safe_max_tokens(len(prompt_ids))
    if safe_mt < 100:
        return {
            "text": "",
            "prompt_tokens": len(prompt_ids),
            "completion_tokens": 0,
            "finish_reason": "prompt_too_long",
        }
    payload = {
        "model": MODEL_NAME,
        "prompt": prompt_ids,
        "max_tokens": min(max_tokens, safe_mt),
        "temperature": temperature,
        "stop_token_ids": stop_token_ids,
        "repetition_detection": {
            "max_pattern_size": 100,
            "min_count": 3,
        },
    }
    if seed is not None:
        payload["seed"] = seed
    resp = requests.post(f"{base_url}/completions", json=payload, timeout=300)
    resp.raise_for_status()
    data = resp.json()
    choice = data["choices"][0]
    text = choice["text"].rstrip()
    usage = data.get("usage", {})
    finish_reason = choice.get("finish_reason", "stop") or "stop"
    return {
        "text": text,
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens", 0),
        "finish_reason": finish_reason,
    }


def main() -> None:
    task_data = json.loads(Path(TASK_JSON).read_text())
    examples = task_data["examples"]

    print(f"Loading tokenizer: {MODEL_NAME}", flush=True)
    tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME, trust_remote_code=True)
    builder = PromptBuilder(tokenizer=tokenizer)
    print(f"Chat format: {builder.chat_format}", flush=True)

    # Check vLLM is up
    try:
        r = requests.get(f"{BASE_URL}/models", timeout=5)
        r.raise_for_status()
        print(f"vLLM OK: {r.json()['data'][0]['id']}", flush=True)
    except Exception as e:
        print(f"ERROR: Cannot reach vLLM at {BASE_URL}: {e}", file=sys.stderr)
        sys.exit(1)

    log_path = Path(LOG_FILE)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    with open(log_path, "w") as log:
        def w(s: str = "") -> None:
            log.write(s + "\n")
            log.flush()

        for sample_idx in SAMPLE_INDICES:
            ex = examples[sample_idx]
            raw_input = ex["input"]
            target = str(ex["target"])

            # Strip leading "Question: " if present (template already adds it)
            question = raw_input
            if question.startswith("Question: "):
                question = question[len("Question: "):]

            w("=" * 80)
            w(f"SAMPLE {sample_idx}  |  TARGET: {target}")
            w("=" * 80)
            w()
            w(f"QUESTION: {question[:200]}...")
            w()

            # --- 1. Full generation (greedy, no prefix) ---
            w("--- FULL GENERATION (no prefix, greedy) ---")
            prompt_ids = builder.build_prompt_token_ids(question)
            result = generate(BASE_URL, prompt_ids, builder.stop_token_ids, MAX_TOKENS)
            text = result["text"]
            answer = _extract_answer_from_rollout(text)
            is_correct = (
                answer is not None
                and answer.strip().lower() == target.strip().lower()
            )
            w(f"Answer: {answer}  |  Correct: {is_correct}  |  finish_reason: {result['finish_reason']}")
            w(f"[prompt={result['prompt_tokens']}tok, completion={result['completion_tokens']}tok]")
            w()
            w(text)
            w()

            # --- 2. Step breakdown ---
            steps = split_trace_steps(text)
            w(f"--- STEP BREAKDOWN ({len(steps)} steps) ---")
            for i, step in enumerate(steps, 1):
                preview = step[:100].replace("\n", " ")
                w(f"  Step {i}: {preview}...")
            w()

            # --- 3. MC estimation at midpoint ---
            mid = max(1, len(steps) // 2)
            prefix = steps[:mid]
            w(f"--- MC ESTIMATION at step {mid} (prefix = steps 1..{mid}) ---")
            correct_count = 0
            truncated_count = 0
            for ri in range(1, NUM_ROLLOUTS + 1):
                prefix_ids = builder.build_prompt_token_ids(question, prefix_steps=prefix)
                r = generate(
                    BASE_URL, prefix_ids, builder.stop_token_ids, MAX_TOKENS,
                    temperature=0.7, seed=42 + ri + sample_idx * 100,
                )
                r_answer = _extract_answer_from_rollout(r["text"])
                r_correct = (
                    r_answer is not None
                    and r_answer.strip().lower() == target.strip().lower()
                )
                is_trunc = r["finish_reason"] == "length"
                if r_correct:
                    correct_count += 1
                if is_trunc and r_answer is None:
                    truncated_count += 1
                trunc_tag = " [TRUNCATED]" if is_trunc else ""
                w(f"  Rollout {ri}: answer={r_answer}  correct={r_correct}  finish={r['finish_reason']}{trunc_tag}")

            scorable = NUM_ROLLOUTS - truncated_count
            if scorable > 0:
                mc = correct_count / scorable
                w(f"  MC(step {mid}) = {correct_count}/{scorable} = {mc:.3f}  (excluded {truncated_count} truncated)")
            else:
                w(f"  MC(step {mid}) = 0/0 = N/A  (all {NUM_ROLLOUTS} truncated)")
            w()

            # --- 4. Continuation from step 1 only ---
            w("--- CONTINUATION from step 1 only ---")
            step1_prefix = steps[:1]
            prefix_ids = builder.build_prompt_token_ids(question, prefix_steps=step1_prefix)
            r = generate(BASE_URL, prefix_ids, builder.stop_token_ids, MAX_TOKENS)
            r_answer = _extract_answer_from_rollout(r["text"])
            r_correct = (
                r_answer is not None
                and r_answer.strip().lower() == target.strip().lower()
            )
            w(f"  Prefix: {steps[0][:120]}...")
            w(f"  Answer: {r_answer}  |  Correct: {r_correct}  |  finish_reason: {r['finish_reason']}")
            w(f"  [prompt={r['prompt_tokens']}tok, completion={r['completion_tokens']}tok]")
            w(f"  Continuation text (first 300 chars):")
            w(f"  {r['text'][:300]}")
            w()
            w()

            print(
                f"Sample {sample_idx}: answer={answer} correct={is_correct} "
                f"steps={len(steps)} finish={result['finish_reason']}",
                flush=True,
            )

    print(f"\nDone. Log written to: {log_path}")


if __name__ == "__main__":
    main()
