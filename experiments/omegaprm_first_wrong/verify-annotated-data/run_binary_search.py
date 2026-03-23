"""
Pure binary search on pre-existing steps from human-annotated merged data.

For each sample:
  - steps are taken directly from data/{split}.json (already merged)
  - binary search probes prefix[0..mid] by calling model k rollouts → MC estimate
  - first_wrong = leftmost step where MC drops to 0
  - compare against human annotation (first_wrong_step_index_1based)

Usage (from repo root):
    python -m experiments.omegaprm_first_wrong."verify-annotated-data".run_binary_search \
        --split val --indices 4 \
        --model deepseek-ai/DeepSeek-R1-Distill-Llama-8B \
        --ports 8000 --k-rollouts 16
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

from experiments.omegaprm_first_wrong.rollout import PromptBuilder, VLLMRolloutClient

DATA_DIR = Path(__file__).parent / "data"
OUT_DIR = Path(__file__).parent / "logs"

DEFAULT_MODEL = "deepseek-ai/DeepSeek-R1-Distill-Llama-8B"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--split", default="val", choices=["train", "val", "test"])
    p.add_argument("--indices", default="",
                   help="Comma-separated 0-based list indices. Empty = all wrong traces.")
    p.add_argument("--wrong-only", action="store_true", default=True)
    p.add_argument("--all-traces", dest="wrong_only", action="store_false")
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--tokenizer", default="")
    p.add_argument("--chat-format", default="auto")
    p.add_argument("--ports", default="8000")
    p.add_argument("--base-urls", default="")
    p.add_argument("--temperature", type=float, default=0.6)
    p.add_argument("--top-p", type=float, default=0.95)
    p.add_argument("--max-rollout-tokens", type=int, default=8192)
    p.add_argument("--max-context", type=int, default=32768)
    p.add_argument("--context-margin", type=int, default=512)
    p.add_argument("--timeout-sec", type=int, default=300)
    p.add_argument("--max-parallel", type=int, default=0)
    p.add_argument("--k-rollouts", type=int, default=8)
    p.add_argument("--out-prefix", default="binsearch_annotated")
    p.add_argument("--resume-tag", default="")
    p.add_argument("--dry-run", action="store_true",
                   help="Print prompt text for first sample then exit, no model calls.")
    p.add_argument("--parallel-samples", type=int, default=1,
                   help="Number of items to process in parallel (default: 1).")
    return p.parse_args()


def parse_base_urls(args: argparse.Namespace) -> list[str]:
    if args.base_urls.strip():
        return [u.strip().rstrip("/") for u in args.base_urls.split(",") if u.strip()]
    return [f"http://127.0.0.1:{p.strip()}/v1" for p in args.ports.split(",") if p.strip()]


def mc_at_prefix(
    client: VLLMRolloutClient,
    question: str,
    target: str,
    prefix_steps: list[str],
    k: int,
    dry_run: bool = False,
) -> float:
    """Estimate MC by sampling k rollouts from the given prefix.

    When dry_run=True, prints the prompt text and returns -1 without calling the model.
    """
    if dry_run:
        prompt_text = client.prompt_builder.build_prompt_text(question, prefix_steps or None)
        print(f"\n{'='*60}", flush=True)
        print(f"[DRY RUN] prefix_len={len(prefix_steps)} steps", flush=True)
        print(prompt_text[:2000], flush=True)
        if len(prompt_text) > 2000:
            print(f"... (truncated, total {len(prompt_text)} chars)", flush=True)
        print(f"{'='*60}\n", flush=True)
        return -1.0

    rollouts = client.sample_rollouts(
        question=question,
        prefix_steps=prefix_steps,
        target=target,
        num_rollouts=k,
    )
    scorable = [r for r in rollouts if not (r.is_truncated and not r.is_correct)]
    if not scorable:
        return 0.0
    return sum(1 for r in scorable if r.is_correct) / len(scorable)


def binary_search_steps(
    client: VLLMRolloutClient,
    question: str,
    target: str,
    steps: list[str],
    k: int,
    dry_run: bool = False,
) -> dict[str, Any]:
    """
    Binary search over steps to find first wrong step.

    Returns dict with: predicted_first_wrong_1based, probes
    """
    probes: list[dict] = []

    if dry_run:
        # Print prompt for a few key prefixes: empty, mid, last
        checkpoints = sorted({0, len(steps) // 2, len(steps)})
        for cp in checkpoints:
            mc_at_prefix(client, question, target, steps[:cp], k, dry_run=True)
        return {"predicted_first_wrong_1based": None, "probes": []}

    # Step 0: probe empty prefix to check if model can solve from scratch.
    # Without this, we cannot distinguish "truly too hard" (root mc=0) from
    # "step 1 is wrong" (root mc>0 but first step corrupts the chain).
    root_mc = mc_at_prefix(client, question, target, [], k)
    probes.append({"prefix_len": 0, "mc": root_mc})
    print(f"    probe prefix_len=0/{len(steps)} mc={root_mc:.3f}  [root]", flush=True)

    if root_mc == 0.0:
        # Model cannot solve this question even without any steps → truly too hard.
        return {"predicted_first_wrong_1based": None, "probes": probes, "all_mc_zero": True}

    # Step 1: probe the full sequence to establish that an error exists.
    full_mc = mc_at_prefix(client, question, target, steps, k)
    probes.append({"prefix_len": len(steps), "mc": full_mc})
    print(f"    probe prefix_len={len(steps)}/{len(steps)} mc={full_mc:.3f}  [init]", flush=True)

    if full_mc > 0:
        # Model is correct even with all steps — no error found.
        return {"predicted_first_wrong_1based": None, "probes": probes, "all_mc_zero": False}

    # Invariant: mc(steps[:lo]) > 0  (established: root_mc > 0 when lo=0)
    #            mc(steps[:hi+1]) = 0 (established: full_mc = 0)
    lo, hi = 0, len(steps) - 1
    while lo < hi:
        mid = (lo + hi) // 2
        prefix = steps[: mid + 1]
        mc = mc_at_prefix(client, question, target, prefix, k)
        probes.append({"prefix_len": mid + 1, "mc": mc})
        print(f"    probe prefix_len={mid+1}/{len(steps)} mc={mc:.3f}  [lo={lo} hi={hi}]", flush=True)
        if mc > 0:
            lo = mid + 1
        else:
            hi = mid

    # lo == hi: first_wrong is at position lo (0-based) → 1-based = lo+1.
    # all_mc_zero is always False here since root_mc > 0.
    predicted = lo + 1
    return {"predicted_first_wrong_1based": predicted, "probes": probes, "all_mc_zero": False}


def load_split(split: str) -> list[dict]:
    return json.loads((DATA_DIR / f"{split}.json").read_text())


def select_items(data: list[dict], args: argparse.Namespace) -> list[tuple[int, dict]]:
    if args.indices.strip():
        idxs = [int(i.strip()) for i in args.indices.split(",") if i.strip()]
        return [(i, data[i]) for i in idxs]
    if args.wrong_only:
        return [(i, x) for i, x in enumerate(data) if x["trace_label"] == 1]
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


def main() -> None:
    args = parse_args()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    data = load_split(args.split)
    items = select_items(data, args)
    print(f"Split={args.split} | selected={len(items)}")

    tokenizer_name = args.tokenizer.strip() or args.model
    print(f"Loading tokenizer: {tokenizer_name}", flush=True)
    tokenizer = AutoTokenizer.from_pretrained(tokenizer_name, trust_remote_code=True)
    prompt_builder = PromptBuilder(tokenizer=tokenizer, chat_format=args.chat_format)

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

    done_ids = _load_done_ids(out_path)
    pending = [(i, x) for i, x in items if x["id"] not in done_ids]
    print(f"Done={len(done_ids)} | Pending={len(pending)}")

    meta = {"split": args.split, "model": args.model, "k_rollouts": args.k_rollouts,
            "base_urls": base_urls, "temperature": args.temperature}
    if not out_path.exists():
        with open(out_path, "a") as f:
            f.write(json.dumps({"_meta": meta}) + "\n")

    t_start = time.time()
    _counter_lock = threading.Lock()
    counters = {"exact": 0, "wrong_pred": 0, "no_find": 0, "order": 0}
    out_lock = threading.Lock()
    eval_lock = threading.Lock()

    def _process_one(list_idx: int, item: dict) -> None:
        steps = [s["text"] for s in item["steps"]]
        human_fw = item["first_wrong_step_index_1based"]
        trace_label = item["trace_label"]

        with _counter_lock:
            counters["order"] += 1
            order = counters["order"]

        print(
            f"\n[{order}/{len(pending)}] id={item['id']} subset={item['subset']} "
            f"trace_label={trace_label} human_first_wrong={human_fw} "
            f"n_steps={len(steps)}",
            flush=True,
        )

        try:
            result = binary_search_steps(
                client=client,
                question=item["query"],
                target=item["ground_truth"],
                steps=steps,
                k=args.k_rollouts,
                dry_run=args.dry_run,
            )
        except Exception as exc:
            print(f"  ERROR: {exc}", flush=True)
            return

        if args.dry_run:
            print("Dry run done. Exiting.", flush=True)
            return

        predicted = result["predicted_first_wrong_1based"]
        n_probes = len(result["probes"])
        all_mc_zero = result.get("all_mc_zero", False)

        if trace_label == 1 and human_fw is not None:
            if all_mc_zero:
                eval_tag = "too_hard"
            elif predicted is None:
                eval_tag = "no_find"
                with _counter_lock:
                    counters["no_find"] += 1
            elif predicted == human_fw:
                eval_tag = "exact_match"
                with _counter_lock:
                    counters["exact"] += 1
            else:
                eval_tag = f"off_by_{predicted - human_fw:+d}"
                with _counter_lock:
                    counters["wrong_pred"] += 1
        else:
            eval_tag = "correct_trace"

        print(
            f"  predicted={predicted} | human={human_fw} | eval={eval_tag} "
            f"| n_probes={n_probes}",
            flush=True,
        )

        record: dict[str, Any] = {
            "id": item["id"],
            "list_idx": list_idx,
            "subset": item["subset"],
            "trace_label": trace_label,
            "human_first_wrong_1based": human_fw,
            "predicted_first_wrong_1based": predicted,
            "eval_tag": eval_tag,
            "all_mc_zero": all_mc_zero,
            "n_steps": len(steps),
            "n_probes": n_probes,
            "probes": result["probes"],
        }
        with out_lock:
            with open(out_path, "a") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")

        eval_record = {k: record[k] for k in
                       ["id", "subset", "trace_label", "human_first_wrong_1based",
                        "predicted_first_wrong_1based", "eval_tag", "all_mc_zero", "n_steps", "n_probes"]}
        with eval_lock:
            with open(eval_path, "a") as f:
                f.write(json.dumps(eval_record, ensure_ascii=False) + "\n")

        elapsed = time.time() - t_start
        with _counter_lock:
            snap = dict(counters)
        print(
            f"  elapsed={elapsed:.0f}s | exact={snap['exact']} "
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

    total = counters["exact"] + counters["wrong_pred"] + counters["no_find"]
    print(f"\n=== DONE ===")
    if total > 0:
        print(f"Wrong traces evaluated (excl. too_hard): {total}")
        print(f"  exact_match : {counters['exact']} ({100*counters['exact']/total:.1f}%)")
        print(f"  wrong_pred  : {counters['wrong_pred']} ({100*counters['wrong_pred']/total:.1f}%)")
        print(f"  no_find     : {counters['no_find']} ({100*counters['no_find']/total:.1f}%)")
    print(f"Output: {out_path}")
    print(f"Eval  : {eval_path}")


if __name__ == "__main__":
    main()
