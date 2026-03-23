"""
Top-up correct rollouts for hallu samples that are missing or have < 8 correct traces.
Reads existing combined file, figures out what's needed, generates, writes to a new v2 file.

Usage:
  python -m experiments.omegaprm_first_wrong.gen_correct_topup \
    --combined-jsonl experiments/omegaprm_first_wrong/logs_qc/causal_understanding_combined.jsonl \
    --out-file       experiments/omegaprm_first_wrong/logs_qc/causal_understanding_combined_v2.jsonl
"""
from __future__ import annotations

import argparse
import json
import time
from collections import defaultdict
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
    p.add_argument("--combined-jsonl", required=True)
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
    p.add_argument("--target-correct-per-hallu", type=int, default=8)
    p.add_argument("--max-attempts-multiplier", type=int, default=10,
                   help="Max attempts = target * this (higher for low-mc samples)")
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


def build_correct_entry(
    subset: str,
    sample_id: str,
    dataset_index: int,
    question: str,
    ground_truth: str,
    root_mc: float,
    response_text: str,
    iteration: int,
) -> dict[str, Any]:
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
        "root_mc": root_mc,
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
    correct_texts: list[str] = []
    attempts = 0
    batch = max(4, min(n_needed * 2, 16))

    while len(correct_texts) < n_needed and attempts < max_attempts:
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
            f"  WARNING {label}: only {len(correct_texts)}/{n_needed} correct "
            f"after {attempts} attempts",
            flush=True,
        )
    return correct_texts[:n_needed]


def main() -> None:
    args = parse_args()

    # ------------------------------------------------------------------ #
    # Load existing combined file
    # ------------------------------------------------------------------ #
    print(f"Loading {args.combined_jsonl}...", flush=True)
    existing = load_jsonl(args.combined_jsonl)
    print(f"  Loaded {len(existing)} entries", flush=True)

    # ------------------------------------------------------------------ #
    # Figure out what's needed: hallu samples with < target correct traces
    # ------------------------------------------------------------------ #
    sample_stats: dict[str, dict] = defaultdict(lambda: {
        "error": 0, "correct": 0, "mc": None,
        "subset": None, "question": None, "gt": None, "idx": None,
    })
    for r in existing:
        sid = r["sample_id"]
        sample_stats[sid]["mc"] = r["root_mc"]
        sample_stats[sid]["subset"] = r["subset"]
        sample_stats[sid]["question"] = r["query"]
        sample_stats[sid]["gt"] = r["ground_truth"]
        sample_stats[sid]["idx"] = r["dataset_index"]
        if any(s["step_hallucination"] for s in r["steps"]):
            sample_stats[sid]["error"] += 1
        else:
            sample_stats[sid]["correct"] += 1

    # Only hallu samples (have error traces) that need more correct
    topup_needed = [
        (sid, v)
        for sid, v in sample_stats.items()
        if v["error"] > 0 and v["correct"] < args.target_correct_per_hallu
    ]
    topup_needed.sort(key=lambda x: -x[1]["mc"])  # highest mc first

    total_to_gen = sum(args.target_correct_per_hallu - v["correct"] for _, v in topup_needed)
    print(f"\nSamples needing top-up: {len(topup_needed)}", flush=True)
    print(f"Total correct traces to generate: {total_to_gen}", flush=True)
    for sid, v in topup_needed:
        need = args.target_correct_per_hallu - v["correct"]
        print(f"  {sid}: mc={v['mc']:.3f}, have={v['correct']}, need={need}", flush=True)

    # ------------------------------------------------------------------ #
    # Setup client
    # ------------------------------------------------------------------ #
    base_urls = parse_base_urls(args)
    tokenizer_name = args.tokenizer.strip() or args.model
    print(f"\nLoading tokenizer: {tokenizer_name}", flush=True)
    tokenizer = AutoTokenizer.from_pretrained(tokenizer_name, trust_remote_code=True)
    prompt_builder = PromptBuilder(tokenizer=tokenizer, chat_format=args.chat_format)
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
    # Generate top-up correct traces
    # ------------------------------------------------------------------ #
    new_entries: list[dict] = []
    t0 = time.time()
    for i, (sid, v) in enumerate(topup_needed, 1):
        n_have = v["correct"]
        n_need = args.target_correct_per_hallu - n_have
        max_attempts = n_need * args.max_attempts_multiplier
        print(
            f"\n[{i}/{len(topup_needed)}] {sid} mc={v['mc']:.3f} "
            f"have={n_have} need={n_need}",
            flush=True,
        )
        correct_texts = collect_correct_rollouts(
            client=client,
            question=v["question"],
            target=v["gt"],
            n_needed=n_need,
            max_attempts=max_attempts,
            label=sid,
        )
        # iteration starts after existing correct count
        for iter_idx, resp_text in enumerate(correct_texts, n_have + 1):
            entry = build_correct_entry(
                subset=v["subset"],
                sample_id=sid,
                dataset_index=v["idx"],
                question=v["question"],
                ground_truth=v["gt"],
                root_mc=v["mc"],
                response_text=resp_text,
                iteration=iter_idx,
            )
            new_entries.append(entry)

        elapsed = time.time() - t0
        print(
            f"  cumulative new entries: {len(new_entries)} | elapsed={elapsed:.0f}s",
            flush=True,
        )

    # ------------------------------------------------------------------ #
    # Write v2: existing + new
    # ------------------------------------------------------------------ #
    out_path = Path(args.out_file)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    all_entries = existing + new_entries
    print(
        f"\nWriting {len(existing)} existing + {len(new_entries)} new "
        f"= {len(all_entries)} total → {out_path}",
        flush=True,
    )
    with open(out_path, "w") as f:
        for entry in all_entries:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    print("Done.", flush=True)


if __name__ == "__main__":
    main()
