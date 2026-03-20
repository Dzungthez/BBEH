"""
Run DeepSeek-R1-Distill-Qwen-32B on all 200 bbeh_multistep_arithmetic examples.
Goal: get >= 50 wrong samples for verifier experiments.
"""
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import requests

from bbeh.evaluate import evaluate_correctness

BASE_URL = "http://127.0.0.1:8000/v1"
MODEL = "deepseek-ai/DeepSeek-R1-Distill-Qwen-32B"
TIMEOUT = 60 * 30
MAX_TOKENS = 16384
TEMPERATURE = 0.0
CONCURRENCY = int(os.getenv("CONCURRENCY", "16"))

OUT_DIR = Path("runs/deepseek_r1_multistep_full")
OUT_PATH = OUT_DIR / "predictions.jsonl"
SUMMARY_PATH = OUT_DIR / "summary.json"
TASK_JSON = Path("bbeh/benchmark_tasks/bbeh_multistep_arithmetic/task.json")


def build_user_prompt(question: str) -> str:
    return (
        "Please answer the following question step by step.\n\n"
        "Separate each reasoning step with a blank line (\\n\\n).\n"
        "Provide your final answer at the end in the format: \\boxed{YOUR_ANSWER}\n\n"
        f"Question:\n\n{question}"
    )


def wait_for_server() -> None:
    for _ in range(240):
        try:
            resp = requests.get(f"{BASE_URL}/models", timeout=10)
            if resp.ok:
                return
        except Exception:
            pass
        time.sleep(5)
    raise RuntimeError("vLLM server not ready")


def chat_completion(prompt: str, retries: int = 4) -> str:
    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": TEMPERATURE,
        "max_tokens": MAX_TOKENS,
    }
    last_err: Any = None
    for attempt in range(retries):
        try:
            resp = requests.post(
                f"{BASE_URL}/chat/completions",
                json=payload,
                timeout=TIMEOUT,
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]
        except Exception as exc:
            last_err = exc
            time.sleep(2 ** attempt)
    raise RuntimeError(f"chat_completion failed after {retries} retries: {last_err}")


def run_one(job: dict) -> dict:
    t0 = time.time()
    response = chat_completion(job["prompt"])
    duration = round(time.time() - t0, 3)
    is_correct = evaluate_correctness(
        sample=response,
        reference=str(job["target"]),
    )
    return {
        "task": job["task"],
        "index": job["index"],
        "target": job["target"],
        "prompt": job["prompt"],
        "response": response,
        "is_correct": is_correct,
        "duration_sec": duration,
    }


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    task_data = json.loads(TASK_JSON.read_text())
    examples = task_data["examples"]

    jobs = []
    for idx, ex in enumerate(examples):
        jobs.append({
            "task": "bbeh_multistep_arithmetic",
            "index": idx,
            "target": int(ex["target"]),
            "prompt": build_user_prompt(ex["input"]),
        })

    print(f"Total jobs: {len(jobs)}")
    wait_for_server()

    total = correct = 0
    durations = []
    start_all = time.time()

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
                    }
                fout.write(json.dumps(rec, ensure_ascii=False) + "\n")
                fout.flush()
                total += 1
                if rec["is_correct"]:
                    correct += 1
                if rec["duration_sec"] is not None:
                    durations.append(rec["duration_sec"])
                wrong_so_far = total - correct
                print(
                    f"[{total:03d}/200] idx={rec['index']} correct={rec['is_correct']} "
                    f"acc={correct}/{total} wrong={wrong_so_far}",
                    flush=True,
                )

    elapsed = time.time() - start_all
    summary = {
        "model": MODEL,
        "max_tokens": MAX_TOKENS,
        "concurrency": CONCURRENCY,
        "total": total,
        "correct": correct,
        "wrong": total - correct,
        "accuracy": correct / total if total else 0.0,
        "elapsed_sec": round(elapsed, 3),
        "avg_sec_per_request": round(sum(durations) / len(durations), 3) if durations else None,
    }
    SUMMARY_PATH.write_text(json.dumps(summary, indent=2))
    print("DONE", json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
