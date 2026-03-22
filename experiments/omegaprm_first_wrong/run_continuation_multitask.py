"""Run continuation-style diagnostics on multiple BBEH tasks.

Flow mirrors ``run_continuation_test.py``:
  1) Full generation (no prefix, greedy)
  2) Step breakdown from the full trace
  3) MC estimation at midpoint prefix
  4) Continuation from step-1 prefix

Additionally, this runner computes root MC at empty prefix and applies
``root_filter_status == 'mixed'`` by default (skips too-hard/too-easy cases).

Usage example:
  PYTHONPATH=. python -m experiments.omegaprm_first_wrong.run_continuation_multitask \
    --ports 8000,8001,8002,8003 \
    --model deepseek-ai/DeepSeek-R1-Distill-Qwen-32B
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from itertools import cycle
from pathlib import Path
from typing import Any

import requests
from transformers import AutoTokenizer

from experiments.omegaprm_first_wrong.rollout import (
    PromptBuilder,
    _extract_answer_from_rollout,
)
from experiments.omegaprm_first_wrong.step_splitter import split_trace_steps


DEFAULT_MODEL = "deepseek-ai/DeepSeek-R1-Distill-Qwen-32B"
DEFAULT_TASKS = [
    "bbeh_causal_understanding",
    "bbeh_multistep_arithmetic",
    "bbeh_time_arithmetic",
    "bbeh_word_sorting",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Continuation-style rollout diagnostics across multiple BBEH tasks"
        )
    )
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument(
        "--tokenizer",
        default="",
        help="Tokenizer name/path (defaults to --model).",
    )
    parser.add_argument(
        "--chat-format",
        default="auto",
        help="Prompt format: auto, chatml, deepseek.",
    )
    parser.add_argument("--system-prompt", default="")
    parser.add_argument(
        "--tasks",
        default=",".join(DEFAULT_TASKS),
        help=(
            "Comma-separated task ids under bbeh/benchmark_tasks. "
            "Default: causal_understanding,multistep_arithmetic,"
            "time_arithmetic,word_sorting"
        ),
    )
    parser.add_argument(
        "--sample-indices",
        default="0,1,2,5,10",
        help=(
            "Comma-separated example indices per task. "
            "Set empty string to sample randomly."
        ),
    )
    parser.add_argument(
        "--num-samples-per-task",
        type=int,
        default=5,
        help="Used only when --sample-indices is empty.",
    )
    parser.add_argument("--seed", type=int, default=20260322)
    parser.add_argument(
        "--base-urls",
        default="",
        help="Comma-separated full URLs (e.g., http://127.0.0.1:8000/v1).",
    )
    parser.add_argument(
        "--ports",
        default="8000,8001,8002,8003",
        help="Used when --base-urls is empty.",
    )
    parser.add_argument("--max-tokens", type=int, default=24576)
    parser.add_argument("--max-context", type=int, default=32768)
    parser.add_argument("--context-margin", type=int, default=512)
    parser.add_argument("--timeout-sec", type=int, default=300)
    parser.add_argument("--temperature-full", type=float, default=0.0)
    parser.add_argument("--temperature-rollout", type=float, default=0.6)
    parser.add_argument("--top-p", type=float, default=0.95)
    parser.add_argument("--num-rollouts-root", type=int, default=6)
    parser.add_argument("--num-rollouts-mid", type=int, default=6)
    parser.add_argument(
        "--require-root-mixed",
        action="store_true",
        default=True,
        help="Keep only samples with root_filter_status='mixed'.",
    )
    parser.add_argument(
        "--allow-root-all",
        action="store_true",
        help="Disable root mixed filter (overrides --require-root-mixed).",
    )
    parser.add_argument(
        "--out-dir",
        default="experiments/omegaprm_first_wrong/logs",
    )
    parser.add_argument(
        "--out-prefix",
        default="continuation_4tasks",
    )
    return parser.parse_args()


def parse_base_urls(args: argparse.Namespace) -> list[str]:
    if args.base_urls.strip():
        return [
            item.strip().rstrip("/")
            for item in args.base_urls.split(",")
            if item.strip()
        ]
    return [
        f"http://127.0.0.1:{port.strip()}/v1"
        for port in args.ports.split(",")
        if port.strip()
    ]


def parse_task_ids(raw_tasks: str) -> list[str]:
    tasks = [item.strip() for item in raw_tasks.split(",") if item.strip()]
    if not tasks:
        raise ValueError("No tasks provided")
    normalized: list[str] = []
    for task in tasks:
        if task.startswith("bbeh_"):
            normalized.append(task)
        else:
            normalized.append(f"bbeh_{task}")
    return normalized


def parse_sample_indices(raw: str) -> list[int] | None:
    raw = raw.strip()
    if not raw:
        return None
    return [int(item.strip()) for item in raw.split(",") if item.strip()]


def load_examples(task_id: str) -> list[dict[str, Any]]:
    task_path = Path("bbeh/benchmark_tasks") / task_id / "task.json"
    if not task_path.exists():
        raise FileNotFoundError(f"Task file not found: {task_path}")
    data = json.loads(task_path.read_text())
    return data["examples"]


def normalize_question(raw_input: str) -> str:
    question = raw_input
    if question.startswith("Question: "):
        question = question[len("Question: ") :]
    return question


def safe_max_tokens(
    num_prompt_tokens: int,
    max_tokens: int,
    max_context: int,
    context_margin: int,
) -> int:
    return min(max_tokens, max_context - num_prompt_tokens - context_margin)


def generate_once(
    *,
    base_url: str,
    model: str,
    prompt_ids: list[int],
    stop_token_ids: list[int],
    max_tokens: int,
    max_context: int,
    context_margin: int,
    timeout_sec: int,
    temperature: float,
    top_p: float,
    seed: int | None,
    max_retries: int = 3,
) -> dict[str, Any]:
    safe_mt = safe_max_tokens(
        len(prompt_ids),
        max_tokens=max_tokens,
        max_context=max_context,
        context_margin=context_margin,
    )
    if safe_mt < 100:
        return {
            "text": "",
            "prompt_tokens": len(prompt_ids),
            "completion_tokens": 0,
            "finish_reason": "prompt_too_long",
        }

    payload: dict[str, Any] = {
        "model": model,
        "prompt": prompt_ids,
        "max_tokens": safe_mt,
        "temperature": temperature,
        "top_p": top_p,
        "stop_token_ids": stop_token_ids,
        "repetition_detection": {
            "max_pattern_size": 100,
            "min_count": 3,
        },
    }
    if seed is not None:
        payload["seed"] = seed

    last_err: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.post(
                f"{base_url}/completions",
                json=payload,
                timeout=timeout_sec,
            )
            resp.raise_for_status()
            data = resp.json()
            choice = data["choices"][0]
            text = str(choice["text"]).rstrip()
            usage = data.get("usage", {})
            return {
                "text": text,
                "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens", 0),
                "finish_reason": choice.get("finish_reason", "stop") or "stop",
            }
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            if attempt < max_retries:
                time.sleep(2**attempt)
    raise RuntimeError(
        f"generate_once failed after {max_retries} retries: {last_err}"
    )


def is_answer_correct(answer: str | None, target: str) -> bool:
    if answer is None:
        return False
    return answer.strip().lower() == target.strip().lower()


def _is_incomplete_finish(finish_reason: str) -> bool:
    """Return True if the rollout was cut short (no chance to produce an answer)."""
    return finish_reason in ("length", "repetition")


def compute_mc(
    *,
    question: str,
    target: str,
    prefix_steps: list[str] | None,
    num_rollouts: int,
    prompt_builder: PromptBuilder,
    base_urls: list[str],
    args: argparse.Namespace,
    seed_base: int,
) -> dict[str, Any]:
    prompt_ids = prompt_builder.build_prompt_token_ids(
        question,
        prefix_steps=prefix_steps,
    )

    def _do_rollout(ridx: int, base_url: str) -> dict[str, Any]:
        result = generate_once(
            base_url=base_url,
            model=args.model,
            prompt_ids=prompt_ids,
            stop_token_ids=prompt_builder.stop_token_ids,
            max_tokens=args.max_tokens,
            max_context=args.max_context,
            context_margin=args.context_margin,
            timeout_sec=args.timeout_sec,
            temperature=args.temperature_rollout,
            top_p=args.top_p,
            seed=seed_base + ridx,
        )
        answer = _extract_answer_from_rollout(result["text"])
        correct = is_answer_correct(answer, target)
        incomplete = _is_incomplete_finish(result["finish_reason"])
        return {
            "rollout_index": ridx,
            "answer": answer,
            "is_correct": correct,
            "finish_reason": result["finish_reason"],
            "prompt_tokens": result["prompt_tokens"],
            "completion_tokens": result["completion_tokens"],
            "is_incomplete": incomplete,
        }

    # Build per-rollout (ridx, base_url) pairs, round-robin across URLs
    url_iter = cycle(base_urls)
    tasks = [(ridx, next(url_iter)) for ridx in range(1, num_rollouts + 1)]

    rollout_records: list[dict[str, Any]] = []
    max_workers = min(num_rollouts, len(base_urls))
    max_workers = max(1, max_workers)
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(_do_rollout, ridx, url): ridx
            for ridx, url in tasks
        }
        for future in as_completed(futures):
            rollout_records.append(future.result())

    rollout_records.sort(key=lambda r: r["rollout_index"])

    correct_count = sum(1 for r in rollout_records if r["is_correct"])
    incomplete_no_answer = sum(
        1 for r in rollout_records
        if r["is_incomplete"] and not r["is_correct"]
    )
    scorable = num_rollouts - incomplete_no_answer
    mc = correct_count / scorable if scorable > 0 else 0.0

    return {
        "mc": mc,
        "num_rollouts": num_rollouts,
        "correct_count": correct_count,
        "incomplete_no_answer": incomplete_no_answer,
        "scorable": scorable,
        "rollouts": rollout_records,
    }


def select_indices(
    examples: list[dict[str, Any]],
    sample_indices: list[int] | None,
    num_samples: int,
    seed: int,
) -> list[int]:
    if sample_indices is not None:
        selected = [idx for idx in sample_indices if 0 <= idx < len(examples)]
        if not selected:
            raise RuntimeError("No valid sample index for this task")
        return selected
    random.seed(seed)
    if len(examples) < num_samples:
        return list(range(len(examples)))
    return sorted(random.sample(range(len(examples)), num_samples))


def root_filter_status(mc: float) -> str:
    if mc == 0.0:
        return "too_hard"
    if mc == 1.0:
        return "too_easy"
    return "mixed"


def main() -> None:
    args = parse_args()
    if args.allow_root_all:
        args.require_root_mixed = False

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_json = out_dir / f"{args.out_prefix}_{timestamp}.json"
    out_md = out_dir / f"{args.out_prefix}_{timestamp}.md"

    task_ids = parse_task_ids(args.tasks)
    sample_indices = parse_sample_indices(args.sample_indices)
    base_urls = parse_base_urls(args)
    if not base_urls:
        raise RuntimeError("No base URLs resolved")

    print(f"Loading tokenizer: {args.tokenizer.strip() or args.model}", flush=True)
    tokenizer = AutoTokenizer.from_pretrained(
        args.tokenizer.strip() or args.model,
        trust_remote_code=True,
    )
    prompt_builder = PromptBuilder(
        tokenizer=tokenizer,
        chat_format=args.chat_format,
        system_prompt=args.system_prompt or None,
    )
    print(f"Chat format: {prompt_builder.chat_format}", flush=True)

    # Health-check all configured endpoints once.
    for base_url in base_urls:
        resp = requests.get(f"{base_url}/models", timeout=5)
        resp.raise_for_status()
        model_id = resp.json()["data"][0]["id"]
        print(f"vLLM OK: {base_url} -> {model_id}", flush=True)

    url_cycle = cycle(base_urls)
    report: dict[str, Any] = {
        "meta": {
            "model": args.model,
            "tokenizer": args.tokenizer.strip() or args.model,
            "chat_format": prompt_builder.chat_format,
            "base_urls": base_urls,
            "tasks": task_ids,
            "sample_indices": sample_indices,
            "num_samples_per_task": args.num_samples_per_task,
            "temperature_full": args.temperature_full,
            "temperature_rollout": args.temperature_rollout,
            "top_p": args.top_p,
            "num_rollouts_root": args.num_rollouts_root,
            "num_rollouts_mid": args.num_rollouts_mid,
            "require_root_mixed": args.require_root_mixed,
            "max_tokens": args.max_tokens,
            "max_context": args.max_context,
            "context_margin": args.context_margin,
            "timeout_sec": args.timeout_sec,
            "seed": args.seed,
        },
        "tasks": [],
    }

    for task_pos, task_id in enumerate(task_ids, start=1):
        examples = load_examples(task_id)
        try:
            selected = select_indices(
                examples=examples,
                sample_indices=sample_indices,
                num_samples=args.num_samples_per_task,
                seed=args.seed + task_pos * 1000,
            )
        except RuntimeError as exc:
            print(f"SKIP TASK {task_id}: {exc}", flush=True)
            continue

        print(
            f"Task {task_pos}/{len(task_ids)} {task_id}: "
            f"selected_indices={selected}",
            flush=True,
        )

        task_report: dict[str, Any] = {
            "task": task_id,
            "num_examples_total": len(examples),
            "selected_indices": selected,
            "samples": [],
            "summary": {
                "kept_samples": 0,
                "skipped_root_filter": 0,
            },
        }

        for sample_order, sample_idx in enumerate(selected, start=1):
          try:
            ex = examples[sample_idx]
            question = normalize_question(ex["input"])
            target = str(ex["target"])

            # 1) Full generation (no prefix, greedy)
            full_prompt_ids = prompt_builder.build_prompt_token_ids(question)
            full_result = generate_once(
                base_url=next(url_cycle),
                model=args.model,
                prompt_ids=full_prompt_ids,
                stop_token_ids=prompt_builder.stop_token_ids,
                max_tokens=args.max_tokens,
                max_context=args.max_context,
                context_margin=args.context_margin,
                timeout_sec=args.timeout_sec,
                temperature=args.temperature_full,
                top_p=args.top_p,
                seed=args.seed + task_pos * 100_000 + sample_idx,
            )
            full_text = full_result["text"]
            full_answer = _extract_answer_from_rollout(full_text)
            full_correct = is_answer_correct(full_answer, target)

            # 2) Step breakdown from full trace
            steps = split_trace_steps(full_text)

            # Root MC and root filtering
            root_mc_info = compute_mc(
                question=question,
                target=target,
                prefix_steps=None,
                num_rollouts=args.num_rollouts_root,
                prompt_builder=prompt_builder,
                base_urls=base_urls,
                args=args,
                seed_base=args.seed
                + task_pos * 1_000_000
                + sample_idx * 10_000,
            )
            root_status = root_filter_status(root_mc_info["mc"])

            sample_report: dict[str, Any] = {
                "sample_index": sample_idx,
                "sample_order": sample_order,
                "target": target,
                "full_generation": {
                    "answer": full_answer,
                    "is_correct": full_correct,
                    "finish_reason": full_result["finish_reason"],
                    "prompt_tokens": full_result["prompt_tokens"],
                    "completion_tokens": full_result["completion_tokens"],
                    "response_text": full_text,
                },
                "step_breakdown": {
                    "num_steps": len(steps),
                    "steps": steps,
                },
                "root_mc": root_mc_info,
                "root_filter_status": root_status,
            }

            if args.require_root_mixed and root_status != "mixed":
                task_report["summary"]["skipped_root_filter"] += 1
                sample_report["skipped_by_root_filter"] = True
                task_report["samples"].append(sample_report)
                print(
                    f"  sample {sample_order}/{len(selected)} idx={sample_idx} "
                    f"root_mc={root_mc_info['mc']:.3f} skipped={root_status}",
                    flush=True,
                )
                continue

            sample_report["skipped_by_root_filter"] = False
            task_report["summary"]["kept_samples"] += 1

            # 3) MC estimation at midpoint
            mid = max(1, len(steps) // 2) if steps else 0
            mid_prefix = steps[:mid] if mid > 0 else []
            mid_mc_info = compute_mc(
                question=question,
                target=target,
                prefix_steps=mid_prefix,
                num_rollouts=args.num_rollouts_mid,
                prompt_builder=prompt_builder,
                base_urls=base_urls,
                args=args,
                seed_base=args.seed
                + task_pos * 2_000_000
                + sample_idx * 10_000,
            )
            sample_report["midpoint_mc"] = {
                "mid_index_1based": mid,
                "prefix_num_steps": len(mid_prefix),
                **mid_mc_info,
            }

            # 4) Continuation from step-1 only
            step1_prefix = steps[:1] if steps else []
            step1_prompt_ids = prompt_builder.build_prompt_token_ids(
                question,
                prefix_steps=step1_prefix,
            )
            step1_result = generate_once(
                base_url=next(url_cycle),
                model=args.model,
                prompt_ids=step1_prompt_ids,
                stop_token_ids=prompt_builder.stop_token_ids,
                max_tokens=args.max_tokens,
                max_context=args.max_context,
                context_margin=args.context_margin,
                timeout_sec=args.timeout_sec,
                temperature=args.temperature_full,
                top_p=args.top_p,
                seed=args.seed + task_pos * 3_000_000 + sample_idx,
            )
            step1_answer = _extract_answer_from_rollout(step1_result["text"])
            step1_correct = is_answer_correct(step1_answer, target)
            sample_report["step1_continuation"] = {
                "prefix_step_text": step1_prefix[0] if step1_prefix else None,
                "answer": step1_answer,
                "is_correct": step1_correct,
                "finish_reason": step1_result["finish_reason"],
                "prompt_tokens": step1_result["prompt_tokens"],
                "completion_tokens": step1_result["completion_tokens"],
                "response_text": step1_result["text"],
            }

            task_report["samples"].append(sample_report)
            print(
                f"  sample {sample_order}/{len(selected)} idx={sample_idx} "
                f"root_mc={root_mc_info['mc']:.3f} mid_mc={mid_mc_info['mc']:.3f}",
                flush=True,
            )
          except Exception as exc:  # noqa: BLE001
            print(
                f"  sample {sample_order}/{len(selected)} idx={sample_idx} "
                f"ERROR: {exc}",
                flush=True,
            )
            task_report["samples"].append({
                "sample_index": sample_idx,
                "sample_order": sample_order,
                "error": str(exc),
            })

        report["tasks"].append(task_report)

    out_json.write_text(json.dumps(report, indent=2, ensure_ascii=False))

    md_lines = [
        "# Continuation Multitask Report",
        "",
        f"- Model: `{args.model}`",
        f"- Chat format: `{prompt_builder.chat_format}`",
        f"- Base URLs: `{base_urls}`",
        f"- Tasks: `{task_ids}`",
        f"- Require root mixed: `{args.require_root_mixed}`",
        "",
    ]
    for task in report["tasks"]:
        summary = task["summary"]
        md_lines.append(f"## {task['task']}")
        md_lines.append(f"- Selected samples: `{len(task['selected_indices'])}`")
        md_lines.append(f"- Kept samples: `{summary['kept_samples']}`")
        md_lines.append(
            f"- Skipped by root filter: `{summary['skipped_root_filter']}`"
        )
        md_lines.append("")
    out_md.write_text("\n".join(md_lines), encoding="utf-8")

    print(f"WROTE_JSON: {out_json}")
    print(f"WROTE_MD: {out_md}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        raise
