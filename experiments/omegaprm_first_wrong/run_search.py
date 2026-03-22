from __future__ import annotations

import argparse
import json
import random
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from experiments.multistep_react_verifier.arithmetic_tools import MultiStepArithmeticEngine, validate_engine_on_multistep_dataset
from experiments.omegaprm_first_wrong.rollout import VLLMRolloutClient
from experiments.omegaprm_first_wrong.search import OmegaPRMFirstWrongSearch
from experiments.omegaprm_first_wrong.step_splitter import merge_short_steps, split_trace_steps


DEFAULT_PREDICTIONS = "runs/deepseek_r1_multistep_full/predictions.jsonl"
DEFAULT_TASK_JSON = "bbeh/benchmark_tasks/bbeh_multistep_arithmetic/task.json"
DEFAULT_MODEL = "deepseek-ai/DeepSeek-R1-Distill-Qwen-32B"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="OmegaPRM-style first-wrong-step search for BBEH multistep arithmetic.")
    parser.add_argument("--predictions-jsonl", default=DEFAULT_PREDICTIONS)
    parser.add_argument("--task-json", default=DEFAULT_TASK_JSON)
    parser.add_argument("--num-samples", type=int, default=5)
    parser.add_argument("--seed", type=int, default=20250322)
    parser.add_argument("--only-wrong-samples", dest="only_wrong_samples", action="store_true")
    parser.add_argument("--include-correct-samples", dest="only_wrong_samples", action="store_false")
    parser.set_defaults(only_wrong_samples=True)
    parser.add_argument("--dataset-indices", default="", help="Comma-separated dataset indices to run instead of random sampling.")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--base-urls", default="")
    parser.add_argument("--ports", default="8000", help="Comma-separated ports used when --base-urls is empty.")
    parser.add_argument("--temperature", type=float, default=0.7)
    parser.add_argument("--max-rollout-tokens", type=int, default=4096)
    parser.add_argument("--timeout-sec", type=int, default=900)
    parser.add_argument("--assistant-prefix-content", default="")
    parser.add_argument("--k-rollouts", type=int, default=8)
    parser.add_argument("--search-limit", type=int, default=20)
    parser.add_argument("--alpha", type=float, default=0.5)
    parser.add_argument("--beta", type=float, default=0.9)
    parser.add_argument("--length-penalty-L", type=int, default=500)
    parser.add_argument("--c-puct", type=float, default=0.125)
    parser.add_argument("--require-root-mixed", action="store_true", help="Skip samples whose root MC is 0 or 1, like the paper's easy/hard filtering.")
    parser.add_argument("--merge-min-tokens", type=int, default=120)
    parser.add_argument("--out-dir", default="experiments/omegaprm_first_wrong/logs")
    parser.add_argument("--out-prefix", default="omegaprm_first_wrong")
    return parser.parse_args()


def parse_base_urls(args: argparse.Namespace) -> list[str]:
    if args.base_urls.strip():
        return [item.strip().rstrip("/") for item in args.base_urls.split(",") if item.strip()]
    return [f"http://127.0.0.1:{port.strip()}/v1" for port in args.ports.split(",") if port.strip()]


def load_trace_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("task") == "bbeh_multistep_arithmetic":
            rows.append(row)
    return rows


def select_rows(rows: list[dict[str, Any]], args: argparse.Namespace) -> list[dict[str, Any]]:
    if args.dataset_indices.strip():
        chosen = {int(item.strip()) for item in args.dataset_indices.split(",") if item.strip()}
        selected = [row for row in rows if int(row["index"]) in chosen]
        if not selected:
            raise RuntimeError(f"No rows found for dataset_indices={sorted(chosen)}")
        return sorted(selected, key=lambda row: int(row["index"]))

    pool = rows
    if args.only_wrong_samples:
        pool = [row for row in rows if not bool(row.get("is_correct", False))]
    if len(pool) < args.num_samples:
        raise RuntimeError(f"Not enough candidate rows: requested={args.num_samples}, available={len(pool)}")
    random.seed(args.seed)
    return random.sample(pool, args.num_samples)


def main() -> None:
    args = parse_args()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    validation = validate_engine_on_multistep_dataset(args.task_json)
    if validation["accuracy"] < 1.0:
        raise RuntimeError(f"Arithmetic engine mismatch: {validation}")

    task_data = json.loads(Path(args.task_json).read_text())
    examples = task_data["examples"]
    trace_rows = load_trace_rows(Path(args.predictions_jsonl))
    selected_rows = select_rows(trace_rows, args)
    base_urls = parse_base_urls(args)

    client = VLLMRolloutClient(
        base_urls=base_urls,
        model=args.model,
        temperature=args.temperature,
        max_tokens=args.max_rollout_tokens,
        timeout_sec=args.timeout_sec,
        assistant_prefix_content=args.assistant_prefix_content or None,
    )

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_json = out_dir / f"{args.out_prefix}_{timestamp}.json"
    out_md = out_dir / f"{args.out_prefix}_{timestamp}.md"

    report: dict[str, Any] = {
        "meta": {
            "predictions_jsonl": args.predictions_jsonl,
            "task_json": args.task_json,
            "model": args.model,
            "base_urls": base_urls,
            "num_samples": len(selected_rows),
            "seed": args.seed,
            "only_wrong_samples": args.only_wrong_samples,
            "k_rollouts": args.k_rollouts,
            "search_limit": args.search_limit,
            "alpha": args.alpha,
            "beta": args.beta,
            "length_penalty_L": args.length_penalty_L,
            "c_puct": args.c_puct,
            "merge_min_tokens": args.merge_min_tokens,
            "engine_validation": validation,
        },
        "samples": [],
    }

    first_wrong_counter = Counter()
    skipped_root_filter = 0
    kept_samples = 0
    for sample_order, row in enumerate(selected_rows, start=1):
        dataset_index = int(row["index"])
        question = examples[dataset_index]["input"]
        target = str(examples[dataset_index]["target"])
        engine = MultiStepArithmeticEngine.from_question(question)
        truth = engine.compute_named_values()
        if str(truth["FINAL"]) != target:
            raise RuntimeError(f"Target mismatch at dataset index {dataset_index}: {truth['FINAL']} vs {target}")

        raw_steps = split_trace_steps(row["response"])
        merged_steps = merge_short_steps(raw_steps, args.merge_min_tokens)
        search = OmegaPRMFirstWrongSearch(
            question=question,
            target=target,
            source_steps=merged_steps,
            rollout_client=client,
            k_rollouts=args.k_rollouts,
            search_limit=args.search_limit,
            alpha=args.alpha,
            beta=args.beta,
            length_penalty_L=args.length_penalty_L,
            c_puct=args.c_puct,
        )
        result = search.run()
        root_filter_status = "mixed" if 0.0 < result.root_mc < 1.0 else ("too_hard" if result.root_mc == 0.0 else "too_easy")
        if args.require_root_mixed and root_filter_status != "mixed":
            skipped_root_filter += 1
            print(
                f"sample {sample_order}/{len(selected_rows)} | idx={dataset_index} | root_mc={result.root_mc:.3f} | skipped={root_filter_status}",
                flush=True,
            )
            continue

        kept_samples += 1
        source_first_wrong = result.source_first_wrong.first_wrong_step_index_1based if result.source_first_wrong else None
        if source_first_wrong is not None:
            first_wrong_counter[source_first_wrong] += 1
        sample_report = {
            "sample_order": sample_order,
            "dataset_index": dataset_index,
            "target": target,
            "source_is_correct": bool(row.get("is_correct", False)),
            "raw_step_count": len(raw_steps),
            "merged_step_count": len(merged_steps),
            "root_mc": result.root_mc,
            "root_filter_status": root_filter_status,
            "source_first_wrong": None if result.source_first_wrong is None else {
                "found": result.source_first_wrong.found,
                "first_wrong_step_index_1based": result.source_first_wrong.first_wrong_step_index_1based,
                "previous_step_index_1based": result.source_first_wrong.previous_step_index_1based,
                "selected_rollout_source": result.source_first_wrong.selected_rollout_source,
                "selected_rollout_num_steps": result.source_first_wrong.selected_rollout_num_steps,
                "search_path": [probe.__dict__ for probe in result.source_first_wrong.search_path],
            },
            "search_limit_used": len(result.selected_rollout_results),
            "selected_rollout_results": [
                {
                    "found": item.found,
                    "first_wrong_step_index_1based": item.first_wrong_step_index_1based,
                    "previous_step_index_1based": item.previous_step_index_1based,
                    "selected_rollout_source": item.selected_rollout_source,
                    "selected_rollout_num_steps": item.selected_rollout_num_steps,
                    "search_path": [probe.__dict__ for probe in item.search_path],
                }
                for item in result.selected_rollout_results
            ],
            "num_states": result.num_states,
            "num_rollouts_total": result.num_rollouts_total,
            "num_candidates_remaining": result.num_candidates_remaining,
            "tree_summary": result.tree_summary,
        }
        report["samples"].append(sample_report)
        print(
            f"sample {sample_order}/{len(selected_rows)} | idx={dataset_index} | root_mc={result.root_mc:.3f} | source_first_wrong={source_first_wrong}",
            flush=True,
        )

    report["overall"] = {
        "num_samples": kept_samples,
        "skipped_root_filter": skipped_root_filter,
        "source_first_wrong_distribution": dict(first_wrong_counter),
    }
    out_json.write_text(json.dumps(report, indent=2, ensure_ascii=False))

    md_lines = [
        "# OmegaPRM First-Wrong-Step Search",
        "",
        f"- Samples: `{len(report['samples'])}`",
        f"- Model: `{args.model}`",
        f"- Base URLs: `{base_urls}`",
        f"- Source predictions: `{args.predictions_jsonl}`",
        f"- First-wrong distribution: `{dict(first_wrong_counter)}`",
        "",
    ]
    for sample in report["samples"]:
        md_lines.append(f"## Dataset index {sample['dataset_index']}")
        md_lines.append(f"- Root MC: `{sample['root_mc']:.3f}`")
        if sample["source_first_wrong"] is None:
            md_lines.append("- Source first wrong: `None`")
        else:
            md_lines.append(f"- Source first wrong: `{sample['source_first_wrong']['first_wrong_step_index_1based']}`")
            md_lines.append(f"- Search path probes: `{sample['source_first_wrong']['search_path']}`")
        md_lines.append(f"- Tree states: `{sample['num_states']}`")
        md_lines.append(f"- Rollouts used: `{sample['num_rollouts_total']}`")
        md_lines.append("")
    out_md.write_text("\n".join(md_lines), encoding="utf-8")

    print(f"WROTE_JSON: {out_json}")
    print(f"WROTE_MD: {out_md}")


if __name__ == "__main__":
    main()
