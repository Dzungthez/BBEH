"""
Generate correct rollouts for:
  1. too_easy samples (100 random) — 4 correct rollouts each → 400 entries
  2. hallu samples (up to 50) — 8 correct rollouts each → 400 entries

Merge with existing 658 hallu entries → single output JSONL.

Usage:
  python -m experiments.omegaprm_first_wrong.gen_correct_rollouts \
    --hallu-jsonl experiments/omegaprm_first_wrong/logs_qc/omegaprm_search_causal_understanding_20260322_133748_hallu.jsonl \
    --orig-jsonl  experiments/omegaprm_first_wrong/logs_qc/omegaprm_search_causal_understanding_20260322_133748.jsonl \
    --out-file    experiments/omegaprm_first_wrong/logs_qc/causal_understanding_combined.jsonl
"""
from __future__ import annotations

import argparse
import json
import random
import time
from pathlib import Path
from typing import Any

from transformers import AutoTokenizer

from experiments.omegaprm_first_wrong.rollout import (
    PromptBuilder,
    VLLMRolloutClient,
    _extract_answer_from_rollout,
)
from experiments.omegaprm_first_wrong.step_splitter import split_trace_steps


DEFAULT_MODEL = "deepseek-ai/DeepSeek-R1-Distill-Qwen-32B"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--hallu-jsonl", required=True)
    p.add_argument("--orig-jsonl", required=True)
    p.add_argument("--out-file", required=True)
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--tokenizer", default="")
    p.add_argument("--chat-format", default="auto")
    p.add_argument("--ports", default="8000,8001,8002,8003")
    p.add_argument("--base-urls", default="")
    p.add_argument("--temperature", type=float, default=0.6)
    p.add_argument("--top-p", type=float, default=0.95)
    p.add_argument("--max-rollout-tokens", type=int, default=20000)
    p.add_argument("--max-context", type=int, default=32768)
    p.add_argument("--context-margin", type=int, default=512)
    p.add_argument("--timeout-sec", type=int, default=900)
    p.add_argument("--max-parallel", type=int, default=32)
    p.add_argument("--seed", type=int, default=42)
    # too_easy config
    p.add_argument("--too-easy-count", type=int, default=100,
                   help="Number of too_easy samples to pick (random)")
    p.add_argument("--too-easy-rollouts", type=int, default=4,
                   help="Correct rollouts required per too_easy sample")
    # hallu config
    p.add_argument("--hallu-rollouts-per-sample", type=int, default=8,
                   help="Target correct rollouts per hallu sample")
    p.add_argument("--hallu-mc-threshold", type=float, default=0.15,
                   help="Skip hallu samples with root_mc <= this threshold")
    p.add_argument("--max-attempts-multiplier", type=int, default=6,
                   help="Max rollout attempts = target * this")
    return p.parse_args()


def parse_base_urls(args: argparse.Namespace) -> list[str]:
    if args.base_urls.strip():
        return [u.strip().rstrip("/") for u in args.base_urls.split(",") if u.strip()]
    return [f"http://127.0.0.1:{p.strip()}/v1" for p in args.ports.split(",") if p.strip()]


def load_jsonl(path: str) -> list[dict]:
    rows = []
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def load_task_examples(task_id: str) -> list[dict]:
    task_path = Path("bbeh/benchmark_tasks") / task_id / "task.json"
    return json.loads(task_path.read_text())["examples"]


def build_correct_entry(
    subset: str,
    sample_id: str,
    dataset_index: int,
    question: str,
    ground_truth: str,
    response_text: str,
    iteration: int,
) -> dict[str, Any]:
    """Build a hallu-format entry where all steps are correct (no hallucination)."""
    steps_raw = split_trace_steps(response_text)
    steps_labeled = [
        {
            "step_id": i,
            "text": text,
            "step_hallucination": False,
            "cumulative_hallucination": False,
        }
        for i, text in enumerate(steps_raw)
    ]
    answer = _extract_answer_from_rollout(response_text)
    return {
        "subset": subset,
        "sample_id": sample_id,
        "dataset_index": dataset_index,
        "iteration": iteration,
        "query": question,
        "response": response_text,
        "steps": steps_labeled,
        "answer": answer,
        "ground_truth": ground_truth,
        "root_mc": 1.0,  # too_easy or high-mc correct
        "first_wrong_step_index_1based": None,
        "rollout_source": "correct_rollout",
    }


def collect_correct_rollouts(
    client: VLLMRolloutClient,
    question: str,
    target: str,
    n_needed: int,
    max_attempts: int,
    label: str,
) -> list[str]:
    """Keep rolling out until we have n_needed correct responses."""
    correct_texts: list[str] = []
    attempts = 0
    batch = max(4, n_needed)

    while len(correct_texts) < n_needed and attempts < max_attempts:
        remaining = n_needed - len(correct_texts)
        this_batch = min(batch, max_attempts - attempts)
        samples = client.sample_rollouts(
            question=question,
            prefix_steps=[],
            target=target,
            num_rollouts=this_batch,
        )
        attempts += this_batch
        for s in samples:
            if s.is_correct:
                correct_texts.append(s.response_text)
                if len(correct_texts) >= n_needed:
                    break
        print(
            f"    {label}: {len(correct_texts)}/{n_needed} correct "
            f"(attempts={attempts}/{max_attempts})",
            flush=True,
        )

    if len(correct_texts) < n_needed:
        print(
            f"  WARNING {label}: only got {len(correct_texts)}/{n_needed} correct "
            f"after {attempts} attempts",
            flush=True,
        )
    return correct_texts[:n_needed]


def main() -> None:
    args = parse_args()
    random.seed(args.seed)

    base_urls = parse_base_urls(args)
    tokenizer_name = args.tokenizer.strip() or args.model
    print(f"Loading tokenizer: {tokenizer_name}", flush=True)
    tokenizer = AutoTokenizer.from_pretrained(tokenizer_name, trust_remote_code=True)
    prompt_builder = PromptBuilder(
        tokenizer=tokenizer,
        chat_format=args.chat_format,
    )
    client = VLLMRolloutClient(
        prompt_builder=prompt_builder,
        base_urls=base_urls,
        model=args.model,
        temperature=args.temperature,
        top_p=args.top_p,
        max_tokens=args.max_rollout_tokens,
        timeout_sec=args.timeout_sec,
        max_context=args.max_context,
        context_safety_margin=args.context_margin,
        max_parallel_requests=args.max_parallel,
    )

    # ------------------------------------------------------------------ #
    # Load existing hallu entries (658 lines)
    # ------------------------------------------------------------------ #
    print("\nLoading existing hallu entries...", flush=True)
    existing_hallu = load_jsonl(args.hallu_jsonl)
    # Filter out any _meta lines
    existing_hallu = [r for r in existing_hallu if "subset" in r]
    print(f"  Existing hallu entries: {len(existing_hallu)}", flush=True)

    # ------------------------------------------------------------------ #
    # Load orig JSONL to get too_easy indices
    # ------------------------------------------------------------------ #
    print("Loading orig JSONL...", flush=True)
    orig_rows = load_jsonl(args.orig_jsonl)
    too_easy_rows = [r for r in orig_rows if r.get("_skipped") == "too_easy"]
    print(f"  Too easy indices available: {len(too_easy_rows)}", flush=True)

    # Determine task from meta
    meta_row = next((r for r in orig_rows if "_meta" in r), None)
    task_id = meta_row["_meta"]["tasks"][0] if meta_row else "bbeh_causal_understanding"
    subset = task_id
    examples = load_task_examples(task_id)

    # ------------------------------------------------------------------ #
    # Part 1: too_easy — 100 random samples, 4 correct rollouts each
    # ------------------------------------------------------------------ #
    n_too_easy = min(args.too_easy_count, len(too_easy_rows))
    selected_too_easy = random.sample(too_easy_rows, n_too_easy)
    print(
        f"\n=== Part 1: too_easy — {n_too_easy} samples × {args.too_easy_rollouts} rollouts ===",
        flush=True,
    )

    too_easy_entries: list[dict] = []
    t0 = time.time()
    for i, row in enumerate(selected_too_easy, 1):
        idx = row["dataset_index"]
        raw_input = examples[idx]["input"]
        question = (
            raw_input[len("Question: "):] if raw_input.startswith("Question: ") else raw_input
        )
        target = str(examples[idx]["target"])
        sample_id = f"{task_id}:{idx}"
        max_attempts = args.too_easy_rollouts * args.max_attempts_multiplier

        print(
            f"  [{i}/{n_too_easy}] {sample_id} target={target!r}",
            flush=True,
        )
        correct_texts = collect_correct_rollouts(
            client=client,
            question=question,
            target=target,
            n_needed=args.too_easy_rollouts,
            max_attempts=max_attempts,
            label=sample_id,
        )
        for iter_idx, resp_text in enumerate(correct_texts, 1):
            entry = build_correct_entry(
                subset=subset,
                sample_id=sample_id,
                dataset_index=idx,
                question=question,
                ground_truth=target,
                response_text=resp_text,
                iteration=iter_idx,
            )
            too_easy_entries.append(entry)

        elapsed = time.time() - t0
        print(
            f"    done: {len(too_easy_entries)} entries so far | elapsed={elapsed:.0f}s",
            flush=True,
        )

    print(f"\nPart 1 done: {len(too_easy_entries)} too_easy correct entries", flush=True)

    # ------------------------------------------------------------------ #
    # Part 2: hallu samples — filter by mc, gen 8 correct rollouts each
    # ------------------------------------------------------------------ #
    # Get unique hallu samples sorted by mc descending (highest mc first = easiest to get correct)
    from collections import defaultdict
    sample_mc: dict[str, float] = {}
    sample_info: dict[str, dict] = {}
    for r in existing_hallu:
        sid = r["sample_id"]
        if sid not in sample_mc:
            sample_mc[sid] = r["root_mc"]
            sample_info[sid] = {
                "dataset_index": r["dataset_index"],
                "question": r["query"],
                "ground_truth": r["ground_truth"],
            }

    # Filter out too-low mc
    hallu_candidates = [
        (sid, mc) for sid, mc in sample_mc.items()
        if mc > args.hallu_mc_threshold
    ]
    hallu_candidates.sort(key=lambda x: -x[1])  # highest mc first
    print(
        f"\n=== Part 2: hallu samples — {len(hallu_candidates)} candidates "
        f"(mc > {args.hallu_mc_threshold}) × {args.hallu_rollouts_per_sample} rollouts ===",
        flush=True,
    )

    hallu_correct_entries: list[dict] = []
    t0 = time.time()
    for i, (sid, mc) in enumerate(hallu_candidates, 1):
        info = sample_info[sid]
        idx = info["dataset_index"]
        question = info["question"]
        target = info["ground_truth"]
        max_attempts = args.hallu_rollouts_per_sample * args.max_attempts_multiplier

        print(
            f"  [{i}/{len(hallu_candidates)}] {sid} mc={mc:.3f} target={target!r}",
            flush=True,
        )
        correct_texts = collect_correct_rollouts(
            client=client,
            question=question,
            target=target,
            n_needed=args.hallu_rollouts_per_sample,
            max_attempts=max_attempts,
            label=sid,
        )
        for iter_idx, resp_text in enumerate(correct_texts, 1):
            entry = build_correct_entry(
                subset=subset,
                sample_id=sid,
                dataset_index=idx,
                question=question,
                ground_truth=target,
                response_text=resp_text,
                iteration=iter_idx,
            )
            entry["root_mc"] = mc  # preserve original mc
            hallu_correct_entries.append(entry)

        elapsed = time.time() - t0
        print(
            f"    done: {len(hallu_correct_entries)} entries so far | elapsed={elapsed:.0f}s",
            flush=True,
        )

    print(f"\nPart 2 done: {len(hallu_correct_entries)} hallu correct entries", flush=True)

    # ------------------------------------------------------------------ #
    # Merge and write output
    # ------------------------------------------------------------------ #
    out_path = Path(args.out_file)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    all_entries = existing_hallu + too_easy_entries + hallu_correct_entries
    print(
        f"\nTotal entries: {len(existing_hallu)} (hallu) "
        f"+ {len(too_easy_entries)} (too_easy correct) "
        f"+ {len(hallu_correct_entries)} (hallu correct) "
        f"= {len(all_entries)}",
        flush=True,
    )
    print(f"Writing to {out_path}...", flush=True)
    with open(out_path, "w") as f:
        for entry in all_entries:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    print("Done.", flush=True)


if __name__ == "__main__":
    main()
