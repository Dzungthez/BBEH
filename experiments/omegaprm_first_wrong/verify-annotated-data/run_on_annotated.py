"""
Run OmegaPRM binary search on human-annotated deepseek_merged data.

Loads data/{split}.json (built by build_data.py), runs the same
OmegaPRM search pipeline as run_search.py, then compares the predicted
first_wrong_step_index_1based against the human annotation.

Usage:
    cd /path/to/bbeh
    python -m experiments.omegaprm_first_wrong.verify-annotated-data.run_on_annotated \
        --split val \
        --indices 0 \
        --model deepseek-ai/DeepSeek-R1-Distill-Llama-8B \
        --ports 8000 \
        --k-rollouts 4 \
        --search-limit 6
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any

from transformers import AutoTokenizer

from experiments.omegaprm_first_wrong.rollout import (
    PromptBuilder,
    VLLMRolloutClient,
    _extract_answer_from_rollout,
)
from experiments.omegaprm_first_wrong.search import OmegaPRMFirstWrongSearch

DATA_DIR = Path(__file__).parent / "data"
OUT_DIR = Path(__file__).parent / "logs"

DEFAULT_MODEL = "deepseek-ai/DeepSeek-R1-Distill-Llama-8B"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--split", default="val", choices=["train", "val", "test"])
    p.add_argument(
        "--indices",
        default="",
        help="Comma-separated 0-based indices into the split. Empty = all wrong traces.",
    )
    p.add_argument("--wrong-only", action="store_true", default=True,
                   help="Only run on wrong traces (trace_label=1). Default: True.")
    p.add_argument("--all-traces", dest="wrong_only", action="store_false",
                   help="Run on all traces (wrong + correct).")
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--tokenizer", default="")
    p.add_argument("--chat-format", default="auto")
    p.add_argument("--ports", default="8000")
    p.add_argument("--base-urls", default="")
    p.add_argument("--temperature", type=float, default=0.6)
    p.add_argument("--top-p", type=float, default=0.95)
    p.add_argument("--max-rollout-tokens", type=int, default=16384)
    p.add_argument("--max-context", type=int, default=32768)
    p.add_argument("--context-margin", type=int, default=512)
    p.add_argument("--timeout-sec", type=int, default=900)
    p.add_argument("--max-parallel", type=int, default=0)
    p.add_argument("--k-rollouts", type=int, default=8)
    p.add_argument("--search-limit", type=int, default=18)
    p.add_argument("--alpha", type=float, default=0.5)
    p.add_argument("--beta", type=float, default=0.9)
    p.add_argument("--length-penalty-L", type=int, default=200)
    p.add_argument("--c-puct", type=float, default=0.125)
    p.add_argument("--out-prefix", default="verify_annotated")
    p.add_argument("--resume-tag", default="")
    p.add_argument("--parallel-samples", type=int, default=1,
                   help="Number of items to process in parallel (default: 1).")
    return p.parse_args()


def parse_base_urls(args: argparse.Namespace) -> list[str]:
    if args.base_urls.strip():
        return [u.strip().rstrip("/") for u in args.base_urls.split(",") if u.strip()]
    return [f"http://127.0.0.1:{p.strip()}/v1" for p in args.ports.split(",") if p.strip()]


def load_split(split: str) -> list[dict]:
    path = DATA_DIR / f"{split}.json"
    return json.loads(path.read_text())


def select_items(data: list[dict], args: argparse.Namespace) -> list[tuple[int, dict]]:
    """Return (list_index, item) pairs to process."""
    if args.indices.strip():
        idxs = [int(i.strip()) for i in args.indices.split(",") if i.strip()]
        return [(i, data[i]) for i in idxs]
    if args.wrong_only:
        return [(i, item) for i, item in enumerate(data) if item["trace_label"] == 1]
    return list(enumerate(data))


def _load_done_ids(path: Path) -> set[int]:
    if not path.exists():
        return set()
    done: set[int] = set()
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if "id" in obj and "_meta" not in obj:
                done.add(int(obj["id"]))
        except (json.JSONDecodeError, KeyError):
            pass
    return done


def majority_vote(results: list[dict]) -> int | None:
    """Return majority-voted first_wrong_step_index_1based from search results."""
    found = [r["first_wrong_step_index_1based"] for r in results if r["found"]]
    if not found:
        return None
    from collections import Counter
    return Counter(found).most_common(1)[0][0]


def main() -> None:
    args = parse_args()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    data = load_split(args.split)
    items = select_items(data, args)
    print(f"Split={args.split} | total items in split={len(data)} | selected={len(items)}")

    tokenizer_name = args.tokenizer.strip() or args.model
    print(f"Loading tokenizer: {tokenizer_name}", flush=True)
    tokenizer = AutoTokenizer.from_pretrained(tokenizer_name, trust_remote_code=True)
    prompt_builder = PromptBuilder(
        tokenizer=tokenizer,
        chat_format=args.chat_format,
    )

    base_urls = parse_base_urls(args)
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
        max_parallel_requests=args.max_parallel or None,
    )

    timestamp = args.resume_tag or datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = OUT_DIR / f"{args.out_prefix}_{args.split}_{timestamp}.jsonl"
    eval_path = OUT_DIR / f"{args.out_prefix}_{args.split}_{timestamp}_eval.jsonl"

    # Resume support
    done_ids = _load_done_ids(out_path)
    pending = [(i, item) for i, item in items if item["id"] not in done_ids]
    print(f"Done={len(done_ids)} | Pending={len(pending)}")

    meta = {
        "split": args.split,
        "model": args.model,
        "base_urls": base_urls,
        "k_rollouts": args.k_rollouts,
        "search_limit": args.search_limit,
        "temperature": args.temperature,
        "top_p": args.top_p,
    }
    if not out_path.exists():
        with open(out_path, "a") as f:
            f.write(json.dumps({"_meta": meta}) + "\n")

    t_start = time.time()
    _counter_lock = threading.Lock()
    counters = {"correct_pred": 0, "wrong_pred": 0, "no_find": 0, "order": 0}
    out_lock = threading.Lock()
    eval_lock = threading.Lock()

    def _process_one(list_idx: int, item: dict) -> None:
        item_id = item["id"]
        question = item["query"]
        target = item["ground_truth"]
        human_first_wrong = item["first_wrong_step_index_1based"]
        trace_label = item["trace_label"]

        with _counter_lock:
            counters["order"] += 1
            order = counters["order"]

        print(
            f"\n[{order}/{len(pending)}] id={item_id} subset={item['subset']} "
            f"trace_label={trace_label} human_first_wrong={human_first_wrong} "
            f"n_merged_steps={item['n_merged_steps']}",
            flush=True,
        )

        try:
            search = OmegaPRMFirstWrongSearch(
                question=question,
                target=target,
                rollout_client=client,
                k_rollouts=args.k_rollouts,
                search_limit=args.search_limit,
                alpha=args.alpha,
                beta=args.beta,
                length_penalty_L=args.length_penalty_L,
                c_puct=args.c_puct,
            )
            result = search.run()
        except Exception as exc:
            print(f"  ERROR: {exc}", flush=True)
            return

        rollout_results = [
            {
                "found": r.found,
                "first_wrong_step_index_1based": r.first_wrong_step_index_1based,
                "previous_step_index_1based": r.previous_step_index_1based,
                "first_wrong_step_text": r.first_wrong_step_text,
                "all_steps": r.all_steps,
                "response_text": r.response_text,
                "selected_rollout_source": r.selected_rollout_source,
                "selected_rollout_num_steps": r.selected_rollout_num_steps,
            }
            for r in result.selected_rollout_results
        ]

        predicted = majority_vote(rollout_results)
        found_count = sum(1 for r in rollout_results if r["found"])

        if trace_label == 1 and human_first_wrong is not None:
            if predicted is None:
                eval_tag = "no_find"
                with _counter_lock:
                    counters["no_find"] += 1
            elif predicted == human_first_wrong:
                eval_tag = "exact_match"
                with _counter_lock:
                    counters["correct_pred"] += 1
            else:
                eval_tag = f"off_by_{predicted - human_first_wrong:+d}"
                with _counter_lock:
                    counters["wrong_pred"] += 1
        else:
            eval_tag = "correct_trace"

        print(
            f"  root_mc={result.root_mc:.3f} | found={found_count}/{len(rollout_results)} "
            f"| predicted={predicted} | human={human_first_wrong} | eval={eval_tag}",
            flush=True,
        )

        record: dict[str, Any] = {
            "id": item_id,
            "list_idx": list_idx,
            "subset": item["subset"],
            "trace_label": trace_label,
            "human_first_wrong_1based": human_first_wrong,
            "predicted_first_wrong_1based": predicted,
            "eval_tag": eval_tag,
            "root_mc": result.root_mc,
            "found_count": found_count,
            "search_iterations": len(rollout_results),
            "num_rollouts_total": result.num_rollouts_total,
            "rollout_results": rollout_results,
        }
        with out_lock:
            with open(out_path, "a") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")

        eval_record = {
            "id": item_id,
            "subset": item["subset"],
            "trace_label": trace_label,
            "human_first_wrong_1based": human_first_wrong,
            "predicted_first_wrong_1based": predicted,
            "eval_tag": eval_tag,
            "root_mc": result.root_mc,
        }
        with eval_lock:
            with open(eval_path, "a") as f:
                f.write(json.dumps(eval_record, ensure_ascii=False) + "\n")

        elapsed = time.time() - t_start
        with _counter_lock:
            snap = dict(counters)
        print(
            f"  elapsed={elapsed:.0f}s | exact={snap['correct_pred']} "
            f"wrong={snap['wrong_pred']} no_find={snap['no_find']}",
            flush=True,
        )

    n_workers = max(1, args.parallel_samples)
    if n_workers == 1:
        for list_idx, item in pending:
            _process_one(list_idx, item)
    else:
        with ThreadPoolExecutor(max_workers=n_workers) as pool:
            futures = {pool.submit(_process_one, li, it): it["id"] for li, it in pending}
            for future in as_completed(futures):
                exc = future.exception()
                if exc is not None:
                    iid = futures[future]
                    print(f"  UNHANDLED ERROR id={iid}: {exc}", flush=True)

    total_wrong_traces = counters["correct_pred"] + counters["wrong_pred"] + counters["no_find"]
    print(f"\n=== DONE ===")
    print(f"Wrong traces evaluated: {total_wrong_traces}")
    if total_wrong_traces > 0:
        print(f"  exact_match : {counters['correct_pred']} ({100*counters['correct_pred']/total_wrong_traces:.1f}%)")
        print(f"  wrong_pred  : {counters['wrong_pred']} ({100*counters['wrong_pred']/total_wrong_traces:.1f}%)")
        print(f"  no_find     : {counters['no_find']} ({100*counters['no_find']/total_wrong_traces:.1f}%)")
    print(f"Output: {out_path}")
    print(f"Eval  : {eval_path}")


if __name__ == "__main__":
    main()
