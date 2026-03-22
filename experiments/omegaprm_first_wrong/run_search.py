from __future__ import annotations

import argparse
import json
import random
from datetime import datetime
from pathlib import Path
from typing import Any

from transformers import AutoTokenizer

from experiments.multistep_react_verifier.arithmetic_tools import (
    MultiStepArithmeticEngine,
    validate_engine_on_multistep_dataset,
)
from experiments.omegaprm_first_wrong.rollout import PromptBuilder, VLLMRolloutClient
from experiments.omegaprm_first_wrong.search import OmegaPRMFirstWrongSearch


DEFAULT_PREDICTIONS = "runs/deepseek_r1_multistep_full/predictions.jsonl"
DEFAULT_TASK_JSON = "bbeh/benchmark_tasks/bbeh_multistep_arithmetic/task.json"
DEFAULT_MODEL = "deepseek-ai/DeepSeek-R1-Distill-Qwen-32B"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="OmegaPRM-style first-wrong-step search for BBEH multistep arithmetic."
    )
    parser.add_argument("--predictions-jsonl", default=DEFAULT_PREDICTIONS)
    parser.add_argument("--task-json", default=DEFAULT_TASK_JSON)
    parser.add_argument("--num-samples", type=int, default=5)
    parser.add_argument("--seed", type=int, default=20250322)
    parser.add_argument(
        "--only-wrong-samples",
        dest="only_wrong_samples",
        action="store_true",
    )
    parser.add_argument(
        "--include-correct-samples",
        dest="only_wrong_samples",
        action="store_false",
    )
    parser.set_defaults(only_wrong_samples=True)
    parser.add_argument(
        "--dataset-indices",
        default="",
        help="Comma-separated dataset indices to run instead of random sampling.",
    )
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument(
        "--tokenizer",
        default="",
        help="HuggingFace tokenizer name/path (defaults to --model).",
    )
    parser.add_argument(
        "--chat-format",
        default="auto",
        help="Chat prompt format: auto, chatml, deepseek (default: auto-detect).",
    )
    parser.add_argument("--system-prompt", default="", help="Optional system prompt.")
    parser.add_argument("--base-urls", default="")
    parser.add_argument(
        "--ports",
        default="8000",
        help="Comma-separated ports used when --base-urls is empty.",
    )
    parser.add_argument("--temperature", type=float, default=0.6)
    parser.add_argument("--top-p", type=float, default=0.95)
    parser.add_argument("--max-rollout-tokens", type=int, default=16384)
    parser.add_argument("--max-context", type=int, default=32768)
    parser.add_argument("--context-margin", type=int, default=512)
    parser.add_argument("--timeout-sec", type=int, default=900)
    parser.add_argument("--k-rollouts", type=int, default=8)
    parser.add_argument("--search-limit", type=int, default=20)
    parser.add_argument("--alpha", type=float, default=0.5)
    parser.add_argument("--beta", type=float, default=0.9)
    parser.add_argument("--length-penalty-L", type=int, default=500)
    parser.add_argument("--c-puct", type=float, default=0.125)
    parser.add_argument(
        "--require-root-mixed",
        action="store_true",
        help=(
            "Skip questions whose root MC is 0 (too hard) or 1 (too easy), "
            "matching the paper's question filtering (Appendix A)."
        ),
    )
    parser.add_argument(
        "--out-dir", default="experiments/omegaprm_first_wrong/logs"
    )
    parser.add_argument("--out-prefix", default="omegaprm_first_wrong")
    return parser.parse_args()


def parse_base_urls(args: argparse.Namespace) -> list[str]:
    if args.base_urls.strip():
        return [
            u.strip().rstrip("/")
            for u in args.base_urls.split(",")
            if u.strip()
        ]
    return [
        f"http://127.0.0.1:{p.strip()}/v1"
        for p in args.ports.split(",")
        if p.strip()
    ]


def load_trace_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("task") == "bbeh_multistep_arithmetic":
            rows.append(row)
    return rows


def select_rows(
    rows: list[dict[str, Any]], args: argparse.Namespace
) -> list[dict[str, Any]]:
    if args.dataset_indices.strip():
        chosen = {
            int(i.strip())
            for i in args.dataset_indices.split(",")
            if i.strip()
        }
        selected = [r for r in rows if int(r["index"]) in chosen]
        if not selected:
            raise RuntimeError(
                f"No rows found for dataset_indices={sorted(chosen)}"
            )
        return sorted(selected, key=lambda r: int(r["index"]))

    pool = rows
    if args.only_wrong_samples:
        pool = [r for r in rows if not bool(r.get("is_correct", False))]
    if len(pool) < args.num_samples:
        raise RuntimeError(
            f"Not enough candidate rows: requested={args.num_samples}, "
            f"available={len(pool)}"
        )
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

    # ---- tokenizer & prompt builder ----------------------------------------
    tokenizer_name = args.tokenizer.strip() or args.model
    print(f"Loading tokenizer: {tokenizer_name}", flush=True)
    tokenizer = AutoTokenizer.from_pretrained(
        tokenizer_name, trust_remote_code=True
    )
    prompt_builder = PromptBuilder(
        tokenizer=tokenizer,
        chat_format=args.chat_format,
        system_prompt=args.system_prompt or None,
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
    )

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_json = out_dir / f"{args.out_prefix}_{timestamp}.json"
    out_md = out_dir / f"{args.out_prefix}_{timestamp}.md"

    report: dict[str, Any] = {
        "meta": {
            "predictions_jsonl": args.predictions_jsonl,
            "task_json": args.task_json,
            "model": args.model,
            "tokenizer": tokenizer_name,
            "chat_format": args.chat_format,
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
            "temperature": args.temperature,
            "top_p": args.top_p,
            "max_rollout_tokens": args.max_rollout_tokens,
            "max_context": args.max_context,
            "require_root_mixed": args.require_root_mixed,
            "engine_validation": validation,
        },
        "samples": [],
    }

    skipped_root_filter = 0
    kept_samples = 0
    for sample_order, row in enumerate(selected_rows, start=1):
        dataset_index = int(row["index"])
        raw_input = examples[dataset_index]["input"]
        question = raw_input[len("Question: "):] if raw_input.startswith("Question: ") else raw_input
        target = str(examples[dataset_index]["target"])
        engine = MultiStepArithmeticEngine.from_question(question)
        truth = engine.compute_named_values()
        if str(truth["FINAL"]) != target:
            raise RuntimeError(
                f"Target mismatch at index {dataset_index}: "
                f"{truth['FINAL']} vs {target}"
            )

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

        if result.root_mc == 0.0:
            root_filter_status = "too_hard"
        elif result.root_mc == 1.0:
            root_filter_status = "too_easy"
        else:
            root_filter_status = "mixed"

        if args.require_root_mixed and root_filter_status != "mixed":
            skipped_root_filter += 1
            print(
                f"sample {sample_order}/{len(selected_rows)} "
                f"| idx={dataset_index} "
                f"| root_mc={result.root_mc:.3f} "
                f"| skipped={root_filter_status}",
                flush=True,
            )
            continue

        kept_samples += 1
        found_count = sum(
            1 for r in result.selected_rollout_results if r.found
        )
        sample_report: dict[str, Any] = {
            "sample_order": sample_order,
            "dataset_index": dataset_index,
            "target": target,
            "source_is_correct": bool(row.get("is_correct", False)),
            "root_mc": result.root_mc,
            "root_filter_status": root_filter_status,
            "search_iterations": len(result.selected_rollout_results),
            "errors_found": found_count,
            "selected_rollout_results": [
                {
                    "found": item.found,
                    "first_wrong_step_index_1based": item.first_wrong_step_index_1based,
                    "previous_step_index_1based": item.previous_step_index_1based,
                    "selected_rollout_source": item.selected_rollout_source,
                    "selected_rollout_num_steps": item.selected_rollout_num_steps,
                    "search_path": [
                        p.__dict__ for p in item.search_path
                    ],
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
            f"sample {sample_order}/{len(selected_rows)} "
            f"| idx={dataset_index} "
            f"| root_mc={result.root_mc:.3f} "
            f"| found_errors={found_count}"
            f"/{len(result.selected_rollout_results)}",
            flush=True,
        )

    report["overall"] = {
        "num_samples": kept_samples,
        "skipped_root_filter": skipped_root_filter,
    }
    out_json.write_text(json.dumps(report, indent=2, ensure_ascii=False))

    md_lines = [
        "# OmegaPRM First-Wrong-Step Search",
        "",
        f"- Samples kept: `{kept_samples}`",
        f"- Samples skipped (root filter): `{skipped_root_filter}`",
        f"- Model: `{args.model}`",
        f"- Base URLs: `{base_urls}`",
        f"- Source predictions: `{args.predictions_jsonl}`",
        "",
    ]
    for sample in report["samples"]:
        md_lines.append(f"## Dataset index {sample['dataset_index']}")
        md_lines.append(f"- Root MC: `{sample['root_mc']:.3f}`")
        md_lines.append(
            f"- Search iterations: `{sample['search_iterations']}`"
        )
        md_lines.append(
            f"- Errors found: "
            f"`{sample['errors_found']}/{sample['search_iterations']}`"
        )
        md_lines.append(f"- Tree states: `{sample['num_states']}`")
        md_lines.append(f"- Rollouts used: `{sample['num_rollouts_total']}`")
        md_lines.append("")
    out_md.write_text("\n".join(md_lines), encoding="utf-8")

    print(f"WROTE_JSON: {out_json}")
    print(f"WROTE_MD: {out_md}")


if __name__ == "__main__":
    main()
