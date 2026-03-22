from __future__ import annotations

import argparse
import json
import random
from datetime import datetime
from pathlib import Path
from typing import Any

from transformers import AutoTokenizer

from experiments.omegaprm_first_wrong.rollout import PromptBuilder, VLLMRolloutClient, _extract_answer_from_rollout
from experiments.omegaprm_first_wrong.search import OmegaPRMFirstWrongSearch


DEFAULT_MODEL = "deepseek-ai/DeepSeek-R1-Distill-Qwen-32B"


DEFAULT_TASKS = [
    "bbeh_causal_understanding",
    "bbeh_multistep_arithmetic",
    "bbeh_time_arithmetic",
    "bbeh_word_sorting",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="OmegaPRM-style first-wrong-step search for BBEH tasks."
    )
    parser.add_argument(
        "--tasks",
        default=",".join(DEFAULT_TASKS),
        help="Comma-separated task ids (e.g. causal_understanding,multistep_arithmetic).",
    )
    parser.add_argument(
        "--predictions-jsonl",
        default="",
        help=(
            "Optional predictions file. When set with --only-wrong-samples, "
            "only runs search on samples the model got wrong."
        ),
    )
    parser.add_argument("--num-samples-per-task", type=int, default=5)
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
    parser.set_defaults(only_wrong_samples=False)
    parser.add_argument(
        "--sample-indices",
        default="",
        help="Comma-separated example indices per task (e.g. 0,1,2,5,10).",
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


def parse_task_ids(raw: str) -> list[str]:
    tasks = [t.strip() for t in raw.split(",") if t.strip()]
    return [t if t.startswith("bbeh_") else f"bbeh_{t}" for t in tasks]


def load_examples(task_id: str) -> list[dict[str, Any]]:
    task_path = Path("bbeh/benchmark_tasks") / task_id / "task.json"
    if not task_path.exists():
        raise FileNotFoundError(f"Task file not found: {task_path}")
    return json.loads(task_path.read_text())["examples"]


def load_wrong_indices(predictions_path: str, task_id: str) -> set[int]:
    """Load indices of wrong predictions from a predictions JSONL file."""
    wrong: set[int] = set()
    for line in Path(predictions_path).read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("task") == task_id and not bool(row.get("is_correct", False)):
            wrong.add(int(row["index"]))
    return wrong


def select_indices(
    examples: list[dict[str, Any]],
    task_id: str,
    args: argparse.Namespace,
) -> list[int]:
    """Select which example indices to run search on."""
    if args.sample_indices.strip():
        return [
            int(i.strip())
            for i in args.sample_indices.split(",")
            if i.strip()
        ]

    all_indices = list(range(len(examples)))

    if args.only_wrong_samples and args.predictions_jsonl.strip():
        wrong = load_wrong_indices(args.predictions_jsonl, task_id)
        all_indices = [i for i in all_indices if i in wrong]

    if len(all_indices) <= args.num_samples_per_task:
        return all_indices

    random.seed(args.seed)
    return sorted(random.sample(all_indices, args.num_samples_per_task))


def _build_hallucination_dataset(report: dict[str, Any]) -> list[dict[str, Any]]:
    dataset: list[dict[str, Any]] = []
    entry_id = 0
    for sample in report["samples"]:
        for iteration_idx, item in enumerate(sample["selected_rollout_results"]):
            if not item["found"]:
                continue
            all_steps: list[str] = item["all_steps"]
            first_wrong_1based: int = item["first_wrong_step_index_1based"]
            steps_labeled = []
            for step_i, text in enumerate(all_steps):
                step_idx_1based = step_i + 1
                is_first_wrong = step_idx_1based == first_wrong_1based
                is_cumulative = step_idx_1based >= first_wrong_1based
                steps_labeled.append({
                    "step_id": step_i,
                    "text": text,
                    "step_hallucination": is_first_wrong,
                    "cumulative_hallucination": is_cumulative,
                })
            response_text = item.get("response_text") or "\n\n".join(all_steps)
            answer = _extract_answer_from_rollout(response_text)
            dataset.append({
                "id": entry_id,
                "subset": sample["task"],
                "model": report["meta"]["model"],
                "query": sample["question"],
                "response": response_text,
                "steps": steps_labeled,
                "answer": answer,
                "ground_truth": sample["target"],
                "omegaprm_meta": {
                    "sample_id": sample["sample_id"],
                    "dataset_index": sample["dataset_index"],
                    "root_mc": sample["root_mc"],
                    "iteration": iteration_idx + 1,
                    "rollout_source": item["selected_rollout_source"],
                    "first_wrong_step_index_1based": first_wrong_1based,
                },
            })
            entry_id += 1
    return dataset


def main() -> None:
    args = parse_args()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    task_ids = parse_task_ids(args.tasks)
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
            "tasks": task_ids,
            "predictions_jsonl": args.predictions_jsonl or None,
            "model": args.model,
            "tokenizer": tokenizer_name,
            "chat_format": prompt_builder.chat_format,
            "base_urls": base_urls,
            "num_samples_per_task": args.num_samples_per_task,
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
        },
        "samples": [],
    }

    global_order = 0
    skipped_root_filter = 0
    kept_samples = 0

    for task_id in task_ids:
        examples = load_examples(task_id)
        indices = select_indices(examples, task_id, args)
        print(
            f"\n{'='*60}\n"
            f"TASK: {task_id}  |  indices: {indices}\n"
            f"{'='*60}",
            flush=True,
        )

        for dataset_index in indices:
            global_order += 1
            raw_input = examples[dataset_index]["input"]
            question = (
                raw_input[len("Question: "):]
                if raw_input.startswith("Question: ")
                else raw_input
            )
            target = str(examples[dataset_index]["target"])

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
                print(
                    f"  ERROR at {task_id}[{dataset_index}]: {exc}",
                    flush=True,
                )
                continue

            if result.root_mc == 0.0:
                root_filter_status = "too_hard"
            elif result.root_mc == 1.0:
                root_filter_status = "too_easy"
            else:
                root_filter_status = "mixed"

            if args.require_root_mixed and root_filter_status != "mixed":
                skipped_root_filter += 1
                print(
                    f"  [{global_order}] {task_id}[{dataset_index}] "
                    f"| root_mc={result.root_mc:.3f} "
                    f"| skipped={root_filter_status}",
                    flush=True,
                )
                continue

            kept_samples += 1
            found_count = sum(
                1 for r in result.selected_rollout_results if r.found
            )
            sample_id = f"{task_id}:{dataset_index}"
            sample_report: dict[str, Any] = {
                "sample_id": sample_id,
                "global_order": global_order,
                "task": task_id,
                "dataset_index": dataset_index,
                "question": question,
                "target": target,
                "root_mc": result.root_mc,
                "root_filter_status": root_filter_status,
                "search_iterations": len(result.selected_rollout_results),
                "errors_found": found_count,
                "selected_rollout_results": [
                    {
                        "found": item.found,
                        "first_wrong_step_index_1based": item.first_wrong_step_index_1based,
                        "previous_step_index_1based": item.previous_step_index_1based,
                        "first_wrong_step_text": item.first_wrong_step_text,
                        "previous_step_text": item.previous_step_text,
                        "all_steps": item.all_steps,
                        "response_text": item.response_text,
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
                f"  [{global_order}] {task_id}[{dataset_index}] "
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
        "",
    ]
    for sample in report["samples"]:
        md_lines.append(
            f"## [{sample['sample_id']}] {sample['task']} index {sample['dataset_index']}"
        )
        md_lines.append(f"- Target: `{sample['target']}`")
        md_lines.append(f"- Root MC: `{sample['root_mc']:.3f}`")
        md_lines.append(
            f"- Errors found: "
            f"`{sample['errors_found']}/{sample['search_iterations']}`"
        )
        md_lines.append(f"- Tree states: `{sample['num_states']}`")
        md_lines.append(f"- Rollouts used: `{sample['num_rollouts_total']}`")
        md_lines.append("")

        for i, item in enumerate(sample["selected_rollout_results"], 1):
            if not item["found"]:
                continue
            step_idx = item["first_wrong_step_index_1based"]
            prev_idx = item["previous_step_index_1based"]
            md_lines.append(f"### Iteration {i}: first wrong = step {step_idx}")
            if item.get("previous_step_text"):
                md_lines.append(f"**Step {prev_idx} (last correct, MC > 0):**")
                md_lines.append("```")
                md_lines.append(item["previous_step_text"])
                md_lines.append("```")
            if item.get("first_wrong_step_text"):
                md_lines.append(f"**Step {step_idx} (first wrong, MC = 0):**")
                md_lines.append("```")
                md_lines.append(item["first_wrong_step_text"])
                md_lines.append("```")
            md_lines.append("")
        md_lines.append("")
    out_md.write_text("\n".join(md_lines), encoding="utf-8")

    hallu_dataset = _build_hallucination_dataset(report)
    out_hallu = out_dir / f"{args.out_prefix}_{timestamp}_hallu.json"
    out_hallu.write_text(json.dumps(hallu_dataset, indent=2, ensure_ascii=False))

    print(f"\nWROTE_JSON: {out_json}")
    print(f"WROTE_MD: {out_md}")
    print(f"WROTE_HALLU: {out_hallu} ({len(hallu_dataset)} entries)")


if __name__ == "__main__":
    main()
