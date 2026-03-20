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
MAX_TOKENS = 4096
TEMPERATURE = 0.0
CONCURRENCY = int(os.getenv("CONCURRENCY", "16"))

MANIFEST_PATH = Path("runs/deepseek_r1_qwen32b/sample_manifest.json")
OUT_PATH = Path("runs/deepseek_r1_qwen32b/predictions.jsonl")
SUMMARY_PATH = Path("runs/deepseek_r1_qwen32b/summary.json")

TASK_JSON_ROOT = Path("bbeh/benchmark_tasks")


# User-provided prompt template

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
    raise RuntimeError("vLLM server is not ready in time")


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
            if resp.ok:
                data = resp.json()
                return data["choices"][0]["message"]["content"]
            last_err = f"HTTP {resp.status_code}: {resp.text[:1000]}"
        except Exception as exc:
            last_err = repr(exc)
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"Failed completion after retries: {last_err}")


def run_one(job: dict[str, Any]) -> dict[str, Any]:
    start = time.time()
    response = chat_completion(job["prompt"])
    duration = time.time() - start
    is_correct = evaluate_correctness(response, job["target"])
    return {
        "task": job["task"],
        "index": job["index"],
        "target": job["target"],
        "prompt": job["prompt"],
        "response": response,
        "is_correct": bool(is_correct),
        "duration_sec": round(duration, 3),
    }


def main() -> None:
    manifest = json.loads(MANIFEST_PATH.read_text())
    if OUT_PATH.exists():
        OUT_PATH.unlink()

    wait_for_server()

    jobs = []
    by_task = {}
    for task_name, info in manifest["tasks"].items():
        task_data = json.loads((TASK_JSON_ROOT / task_name / "task.json").read_text())
        task_examples = task_data["examples"]
        by_task[task_name] = {"correct": 0, "total": 0}
        for idx in info["selected_indices_random_order"]:
            ex = task_examples[idx]
            jobs.append(
                {
                    "task": task_name,
                    "index": idx,
                    "target": ex["target"],
                    "prompt": build_user_prompt(ex["input"]),
                }
            )

    total_jobs = len(jobs)
    total = 0
    correct = 0
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
                by_task[rec["task"]]["total"] += 1
                if rec["is_correct"]:
                    correct += 1
                    by_task[rec["task"]]["correct"] += 1
                if rec["duration_sec"] is not None:
                    durations.append(rec["duration_sec"])

                t_c = by_task[rec["task"]]["correct"]
                t_n = by_task[rec["task"]]["total"]
                print(
                    f"[{total:03d}/{total_jobs}] {rec['task']}#{rec['index']} "
                    f"correct={rec['is_correct']} task_acc={t_c}/{t_n} "
                    f"dur={rec['duration_sec']}",
                    flush=True,
                )

    elapsed = time.time() - start_all
    by_task_out = {}
    for task_name, stat in by_task.items():
        c = stat["correct"]
        n = stat["total"]
        by_task_out[task_name] = {
            "correct": c,
            "total": n,
            "accuracy": c / n if n else 0.0,
        }

    summary = {
        "model": MODEL,
        "concurrency": CONCURRENCY,
        "max_tokens": MAX_TOKENS,
        "num_predictions": total,
        "correct": correct,
        "accuracy": correct / total if total else 0.0,
        "elapsed_sec": round(elapsed, 3),
        "avg_sec_per_response": round(elapsed / total, 3) if total else None,
        "avg_sec_per_successful_request": round(sum(durations) / len(durations), 3)
        if durations
        else None,
        "by_task": by_task_out,
        "output_jsonl": str(OUT_PATH),
    }
    SUMMARY_PATH.write_text(json.dumps(summary, indent=2))
    print("DONE", json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
