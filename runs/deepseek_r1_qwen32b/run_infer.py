"""
Inference + evaluation script for deepseek-ai/DeepSeek-R1-Distill-Qwen-32B on 7 BBEH tasks.

Usage (from repo root):
    python runs/deepseek_r1_qwen32b/run_infer.py

Env vars:
    CONCURRENCY   – parallel requests (default 32)
    MAX_TOKENS    – max completion tokens (default 32768)
    TEMPERATURE   – sampling temperature (default 0.6, per DeepSeek-R1 docs)
"""

import contextlib
import importlib.util
import io as _io
import json
import os
import re
import statistics
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from itertools import cycle
from pathlib import Path
from typing import Any

import requests
import tiktoken

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

PORTS = [int(p.strip()) for p in os.getenv("PORTS", "8000,8001,8002,8003").split(",") if p.strip()]
BASE_URLS = [f"http://127.0.0.1:{p}/v1" for p in PORTS]
MODEL = "deepseek-ai/DeepSeek-R1-Distill-Qwen-32B"
TIMEOUT = 60 * 30
MAX_CONTEXT = int(os.getenv("MAX_CONTEXT", "32768"))
MAX_TOKENS = int(os.getenv("MAX_TOKENS", "32000"))
TEMPERATURE = float(os.getenv("TEMPERATURE", "0.6"))
CONCURRENCY = int(os.getenv("CONCURRENCY", str(max(32, len(PORTS) * 16))))
CONTEXT_SAFETY_MARGIN = int(os.getenv("CONTEXT_SAFETY_MARGIN", "512"))

TASKS = [
    "bbeh_boolean_expressions",
    "bbeh_causal_understanding",
    "bbeh_dyck_languages",
    "bbeh_multistep_arithmetic",
    "bbeh_time_arithmetic",
    "bbeh_web_of_lies",
    "bbeh_word_sorting",
]

TASK_JSON_ROOT = Path("bbeh/benchmark_tasks")
OUT_DIR = Path("runs/deepseek_r1_qwen32b")
OUT_PATH = OUT_DIR / "predictions.jsonl"
SUMMARY_PATH = OUT_DIR / "summary.json"

# Tiktoken encoder for token counting
_enc = tiktoken.get_encoding("cl100k_base")

# ---------------------------------------------------------------------------
# Load evaluate_correctness without side-effect prints
# ---------------------------------------------------------------------------

def _load_evaluate_correctness():
    spec = importlib.util.spec_from_file_location("bbeh_evaluate", Path("bbeh/evaluate.py"))
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    with contextlib.redirect_stdout(_io.StringIO()):
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod.evaluate_correctness

evaluate_correctness = _load_evaluate_correctness()

# ---------------------------------------------------------------------------
# Round-robin URL pool
# ---------------------------------------------------------------------------

import threading

_url_lock = threading.Lock()
_url_cycle = cycle(BASE_URLS)

def _next_url() -> str:
    with _url_lock:
        return next(_url_cycle)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def count_tokens(text: str) -> int:
    return len(_enc.encode(text))


def analyse_steps(response: str) -> dict[str, Any]:
    """Split response into reasoning steps and compute token stats."""
    normalized = response.replace("<think>\n", "").replace("<think>", "").replace("</think>", "")
    steps = [s.strip() for s in normalized.split("\n\n") if s.strip()]
    step_tokens = [count_tokens(s) for s in steps]
    return {
        "num_steps": len(steps),
        "step_tokens": step_tokens,
    }


def build_user_prompt(question: str) -> str:
    # Per DeepSeek-R1 docs: no system prompt, all instructions in user prompt
    # Include boxed directive for math-style problems and explicitly induce thinking mode.
    return (
        "Start your response with exactly '<think>\\n'.\n"
        "Reason step by step. Separate each reasoning step with a blank line (\\n\\n).\n"
        "Put your final answer at the end in the format: \\boxed{YOUR_ANSWER}.\n\n"
        f"Question:\n\n{question}"
    )


# ---------------------------------------------------------------------------
# Server / API
# ---------------------------------------------------------------------------

def wait_for_servers() -> None:
    print("Checking vLLM servers …", flush=True)
    for url in BASE_URLS:
        for _ in range(60):
            try:
                resp = requests.get(f"{url}/models", timeout=10)
                if resp.ok:
                    models = [m["id"] for m in resp.json()["data"]]
                    print(f"  {url} → {models}", flush=True)
                    break
            except Exception:
                pass
            time.sleep(5)
        else:
            raise RuntimeError(f"Server at {url} not ready")
    print("All servers ready.", flush=True)

def _backoff_max_tokens_from_error(error_text: str) -> int | None:
    match = re.search(r"request has (\d+) input tokens", error_text)
    if not match:
        return None
    input_tokens = int(match.group(1))
    safe_max = MAX_CONTEXT - input_tokens - CONTEXT_SAFETY_MARGIN
    if safe_max < 100:
        return None
    return safe_max

def chat_completion(prompt: str, retries: int = 4) -> tuple[str, int]:
    """Return (response_text, completion_tokens)."""
    input_tokens = count_tokens(prompt) + 20
    max_new = min(MAX_TOKENS, MAX_CONTEXT - input_tokens - CONTEXT_SAFETY_MARGIN)
    if max_new < 100:
        raise RuntimeError(f"Prompt too long ({input_tokens} tokens), no room for generation")

    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": TEMPERATURE,
        "max_tokens": max_new,
    }

    last_err: Any = None
    for attempt in range(retries):
        url = _next_url()
        try:
            resp = requests.post(
                f"{url}/chat/completions",
                json=payload,
                timeout=TIMEOUT,
            )
            if resp.ok:
                data = resp.json()
                text = data["choices"][0]["message"]["content"]
                usage = data.get("usage", {})
                comp_tokens = usage.get("completion_tokens", count_tokens(text))
                return text, comp_tokens
            if resp.status_code == 400:
                safe_max = _backoff_max_tokens_from_error(resp.text)
                if safe_max is not None and safe_max < payload["max_tokens"]:
                    payload["max_tokens"] = safe_max
                    last_err = f"Adjusted max_tokens to {safe_max} after context-limit 400"
                    continue
            last_err = f"HTTP {resp.status_code}: {resp.text[:500]}"
        except Exception as exc:
            last_err = repr(exc)
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"Failed after {retries} retries: {last_err}")


# ---------------------------------------------------------------------------
# Per-example worker
# ---------------------------------------------------------------------------

def run_one(job: dict[str, Any]) -> dict[str, Any]:
    start = time.time()
    response, comp_tokens = chat_completion(job["prompt"])
    duration = time.time() - start

    is_correct = evaluate_correctness(response, job["target"])
    step_info = analyse_steps(response)

    return {
        "task": job["task"],
        "index": job["index"],
        "target": job["target"],
        "prompt": job["prompt"],
        "response": response,
        "is_correct": bool(is_correct),
        "duration_sec": round(duration, 3),
        "completion_tokens": comp_tokens,
        "response_chars": len(response),
        "num_steps": step_info["num_steps"],
        "step_tokens": step_info["step_tokens"],
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    wait_for_servers()

    # Build job list
    jobs: list[dict[str, Any]] = []
    for task_name in TASKS:
        task_file = TASK_JSON_ROOT / task_name / "task.json"
        if not task_file.exists():
            print(f"WARNING: {task_file} not found, skipping", file=sys.stderr)
            continue
        task_data = json.loads(task_file.read_text())
        examples = task_data["examples"]
        for idx, ex in enumerate(examples):
            jobs.append(
                {
                    "task": task_name,
                    "index": idx,
                    "target": ex["target"],
                    "prompt": build_user_prompt(ex["input"]),
                }
            )

    total_jobs = len(jobs)
    print(f"Total examples: {total_jobs} across {len(TASKS)} tasks", flush=True)
    print(
        f"Model: {MODEL}  Ports: {PORTS}  Concurrency: {CONCURRENCY}  "
        f"MaxTokens: {MAX_TOKENS}  Temp: {TEMPERATURE}",
        flush=True,
    )

    # Accumulators
    by_task: dict[str, dict] = {
        t: {"correct": 0, "total": 0, "comp_tokens": [], "resp_chars": [],
            "num_steps": [], "all_step_tokens": []}
        for t in TASKS
    }
    total = 0
    correct = 0
    start_all = time.time()

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUT_PATH.open("w", encoding="utf-8") as fout:
        with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
            fut2job = {pool.submit(run_one, j): j for j in jobs}
            for fut in as_completed(fut2job):
                job = fut2job[fut]
                try:
                    rec = fut.result()
                except Exception as exc:
                    rec = {
                        "task": job["task"],
                        "index": job["index"],
                        "target": job["target"],
                        "prompt": job["prompt"],
                        "response": f"__ERROR__: {repr(exc)}",
                        "is_correct": False,
                        "duration_sec": None,
                        "completion_tokens": 0,
                        "response_chars": 0,
                        "num_steps": 0,
                        "step_tokens": [],
                    }

                fout.write(json.dumps(rec, ensure_ascii=False) + "\n")
                fout.flush()

                total += 1
                t = rec["task"]
                by_task[t]["total"] += 1
                by_task[t]["comp_tokens"].append(rec["completion_tokens"])
                by_task[t]["resp_chars"].append(rec["response_chars"])
                by_task[t]["num_steps"].append(rec["num_steps"])
                by_task[t]["all_step_tokens"].extend(rec["step_tokens"])
                if rec["is_correct"]:
                    correct += 1
                    by_task[t]["correct"] += 1

                tc = by_task[t]["correct"]
                tn = by_task[t]["total"]
                print(
                    f"[{total:04d}/{total_jobs}] {t}#{rec['index']:03d} "
                    f"ok={rec['is_correct']} acc={tc}/{tn} "
                    f"tok={rec['completion_tokens']} steps={rec['num_steps']} "
                    f"dur={rec['duration_sec']}s",
                    flush=True,
                )

    elapsed = time.time() - start_all

    # Build summary
    def pct(vals: list[float], p: float) -> float:
        if not vals:
            return 0.0
        sorted_v = sorted(vals)
        idx = int(len(sorted_v) * p / 100)
        idx = min(idx, len(sorted_v) - 1)
        return round(sorted_v[idx], 1)

    def dist(vals: list[float]) -> dict:
        if not vals:
            return {}
        return {
            "min": round(min(vals), 1),
            "p25": pct(vals, 25),
            "median": round(statistics.median(vals), 1),
            "mean": round(statistics.mean(vals), 1),
            "p75": pct(vals, 75),
            "p90": pct(vals, 90),
            "max": round(max(vals), 1),
        }

    by_task_out: dict[str, Any] = {}
    for task_name, stat in by_task.items():
        c = stat["correct"]
        n = stat["total"]
        by_task_out[task_name] = {
            "correct": c,
            "total": n,
            "accuracy": round(c / n, 4) if n else 0.0,
            "response_length_tokens": dist(stat["comp_tokens"]),
            "response_length_chars": dist(stat["resp_chars"]),
            "steps_per_response": dist(stat["num_steps"]),
            "tokens_per_step": dist(stat["all_step_tokens"]),
        }

    all_comp_tokens = [v for s in by_task.values() for v in s["comp_tokens"]]
    all_resp_chars = [v for s in by_task.values() for v in s["resp_chars"]]
    all_steps = [v for s in by_task.values() for v in s["num_steps"]]
    all_step_tokens = [v for s in by_task.values() for v in s["all_step_tokens"]]

    summary = {
        "model": MODEL,
        "ports": PORTS,
        "concurrency": CONCURRENCY,
        "max_tokens": MAX_TOKENS,
        "max_context": MAX_CONTEXT,
        "context_safety_margin": CONTEXT_SAFETY_MARGIN,
        "temperature": TEMPERATURE,
        "num_examples": total,
        "correct": correct,
        "overall_accuracy": round(correct / total, 4) if total else 0.0,
        "elapsed_sec": round(elapsed, 1),
        "overall_response_length_tokens": dist(all_comp_tokens),
        "overall_response_length_chars": dist(all_resp_chars),
        "overall_steps_per_response": dist(all_steps),
        "overall_tokens_per_step": dist(all_step_tokens),
        "by_task": by_task_out,
        "output_jsonl": str(OUT_PATH),
    }

    SUMMARY_PATH.write_text(json.dumps(summary, indent=2))
    print("\n" + "=" * 70)
    print("DONE — Summary")
    print("=" * 70)
    _print_report(summary)


def _print_report(summary: dict) -> None:
    print(f"\nModel : {summary['model']}")
    print(f"Total : {summary['num_examples']} examples  |  "
          f"Correct: {summary['correct']}  |  "
          f"Overall accuracy: {summary['overall_accuracy']:.1%}")
    print(f"Elapsed: {summary['elapsed_sec']}s\n")

    print(f"{'Task':<35} {'Acc':>7}  {'RespTok(med)':>12}  {'Steps(med)':>10}  {'TokPerStep(med)':>15}")
    print("-" * 85)
    for task, s in summary["by_task"].items():
        short = task.replace("bbeh_", "")
        acc = f"{s['accuracy']:.1%}"
        rt = s["response_length_tokens"].get("median", "-")
        ns = s["steps_per_response"].get("median", "-")
        ts = s["tokens_per_step"].get("median", "-")
        print(f"  {short:<33} {acc:>7}  {rt:>12}  {ns:>10}  {ts:>15}")

    print()
    print("Response length (tokens) overall:", summary["overall_response_length_tokens"])
    print("Steps per response overall      :", summary["overall_steps_per_response"])
    print("Tokens per step overall         :", summary["overall_tokens_per_step"])
    print(f"\nFull results → {summary['output_jsonl']}")
    print(f"Summary JSON → {SUMMARY_PATH}")


if __name__ == "__main__":
    main()
