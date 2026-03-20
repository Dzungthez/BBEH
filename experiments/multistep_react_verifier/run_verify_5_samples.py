from __future__ import annotations

import argparse
import json
import random
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from experiments.multistep_react_verifier.arithmetic_tools import (
    MultiStepArithmeticEngine,
    validate_engine_on_multistep_dataset,
)
from experiments.multistep_react_verifier.react_verifier import (
    MultiStepReActStepVerifier,
    VLLMChatClient,
    wait_for_server,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--predictions-jsonl",
        default="runs/deepseek_r1_qwen32b/predictions.jsonl",
        help="Reasoning traces from another LLM.",
    )
    parser.add_argument(
        "--task-json",
        default="bbeh/benchmark_tasks/bbeh_multistep_arithmetic/task.json",
    )
    parser.add_argument("--num-samples", type=int, default=5)
    parser.add_argument("--seed", type=int, default=20250319)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000/v1")
    parser.add_argument("--model", default="Qwen/Qwen3.5-122B-A10B")
    parser.add_argument(
        "--verifier-seed",
        type=int,
        default=None,
        help="Seed passed to the verifier chat completion API.",
    )
    parser.add_argument("--max-react-turns", type=int, default=50)
    parser.add_argument("--max-steps-per-trace", type=int, default=80)
    parser.add_argument("--merge-min-tokens", type=int, default=120)
    parser.add_argument(
        "--only-wrong-samples",
        action="store_true",
        default=True,
        help="Select only traces where original model answer is wrong.",
    )
    parser.add_argument("--context-window", type=int, default=1)
    parser.add_argument(
        "--out-dir",
        default="experiments/multistep_react_verifier/logs",
    )
    parser.add_argument(
        "--out-prefix",
        default="verify_5samples",
        help="Output filename prefix before timestamp.",
    )
    return parser.parse_args()


def load_trace_rows(path: Path) -> list[dict[str, Any]]:
    rows = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        if rec.get("task") == "bbeh_multistep_arithmetic":
            rows.append(rec)
    return rows


def choose_samples(
    trace_rows: list[dict[str, Any]],
    num_samples: int,
    seed: int,
    only_wrong_samples: bool,
) -> list[dict[str, Any]]:
    pool = trace_rows
    if only_wrong_samples:
        pool = [r for r in trace_rows if not bool(r.get("is_correct", False))]
    if len(pool) < num_samples:
        raise RuntimeError(
            f"Not enough candidate traces. required={num_samples}, available={len(pool)}, only_wrong={only_wrong_samples}"
        )
    random.seed(seed)
    return random.sample(pool, num_samples)




def count_tokens(text: str) -> int:
    return len(re.findall(r"\S+", text))


def tail_tokens(text: str, n: int) -> str:
    tokens = re.findall(r"\S+", text)
    if not tokens:
        return ""
    return " ".join(tokens[-n:])


def head_tokens(text: str, n: int) -> str:
    tokens = re.findall(r"\S+", text)
    if not tokens:
        return ""
    return " ".join(tokens[:n])


def merge_short_steps(steps: list[str], min_tokens: int) -> list[str]:
    if min_tokens <= 1 or not steps:
        return [s.strip() for s in steps if s.strip()]

    merged = [s.strip() for s in steps if s.strip()]
    if len(merged) <= 1:
        return merged

    changed = True
    while changed and len(merged) > 1:
        changed = False
        idx = 0
        while idx < len(merged):
            current_len = count_tokens(merged[idx])
            if current_len >= min_tokens or len(merged) == 1:
                idx += 1
                continue

            left_idx = idx - 1 if idx - 1 >= 0 else None
            right_idx = idx + 1 if idx + 1 < len(merged) else None

            if left_idx is None and right_idx is None:
                break

            if left_idx is None:
                merged[right_idx] = merged[idx] + "\n\n" + merged[right_idx]
                del merged[idx]
                changed = True
                continue

            if right_idx is None:
                merged[left_idx] = merged[left_idx] + "\n\n" + merged[idx]
                del merged[idx]
                changed = True
                idx = max(left_idx, 0)
                continue

            left_len = count_tokens(merged[left_idx])
            right_len = count_tokens(merged[right_idx])

            if left_len < right_len:
                merged[left_idx] = merged[left_idx] + "\n\n" + merged[idx]
                del merged[idx]
                changed = True
                idx = max(left_idx, 0)
            else:
                merged[right_idx] = merged[idx] + "\n\n" + merged[right_idx]
                del merged[idx]
                changed = True

    return [m.strip() for m in merged if m.strip()]


def select_steps_for_verification(steps: list[str], limit: int) -> list[str]:
    if limit <= 0 or len(steps) <= limit:
        return steps

    claim_pattern = re.compile(r"(=|\btherefore\b|\bthus\b|\bso\b|\bfinal\b|\banswer\b)", re.IGNORECASE)

    chosen: list[int] = []

    for i in [0, 1, 2, len(steps) - 3, len(steps) - 2, len(steps) - 1]:
        if 0 <= i < len(steps) and i not in chosen:
            chosen.append(i)
            if len(chosen) >= limit:
                break

    if len(chosen) < limit:
        for i, step in enumerate(steps):
            if i in chosen:
                continue
            if claim_pattern.search(step):
                chosen.append(i)
                if len(chosen) >= limit:
                    break

    if len(chosen) < limit:
        for i in range(len(steps)):
            if i not in chosen:
                chosen.append(i)
                if len(chosen) >= limit:
                    break

    chosen = sorted(chosen)
    return [steps[i] for i in chosen]


def selected_step_indices(steps: list[str], limit: int) -> list[int]:
    if limit <= 0 or len(steps) <= limit:
        return list(range(len(steps)))

    claim_pattern = re.compile(r"(=|\btherefore\b|\bthus\b|\bso\b|\bfinal\b|\banswer\b)", re.IGNORECASE)
    chosen: list[int] = []

    for i in [0, 1, 2, len(steps) - 3, len(steps) - 2, len(steps) - 1]:
        if 0 <= i < len(steps) and i not in chosen:
            chosen.append(i)
            if len(chosen) >= limit:
                break

    if len(chosen) < limit:
        for i, step in enumerate(steps):
            if i in chosen:
                continue
            if claim_pattern.search(step):
                chosen.append(i)
                if len(chosen) >= limit:
                    break

    if len(chosen) < limit:
        for i in range(len(steps)):
            if i not in chosen:
                chosen.append(i)
                if len(chosen) >= limit:
                    break

    return sorted(chosen)


def build_context_step_text(
    steps: list[str],
    idx: int,
    window: int,
) -> tuple[str, str]:
    if window <= 0:
        return "", ""

    prev_text = ""
    next_text = ""

    prev_idx = idx - 1
    if prev_idx >= 0:
        prev_text = f"[step {prev_idx + 1}] {tail_tokens(steps[prev_idx], 50)}"

    next_idx = idx + 1
    if next_idx < len(steps):
        next_text = f"[step {next_idx + 1}] {head_tokens(steps[next_idx], 50)}"

    return prev_text, next_text


def main() -> None:
    args = parse_args()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    validation = validate_engine_on_multistep_dataset(args.task_json)
    if validation["accuracy"] < 1.0:
        raise RuntimeError(
            f"Arithmetic engine is not exact on dataset: {validation['correct']}/{validation['total']}"
        )

    trace_rows = load_trace_rows(Path(args.predictions_jsonl))
    if len(trace_rows) < args.num_samples:
        raise RuntimeError(
            f"Not enough traces for multistep_arithmetic: found {len(trace_rows)}"
        )

    selected = choose_samples(
        trace_rows=trace_rows,
        num_samples=args.num_samples,
        seed=args.seed,
        only_wrong_samples=args.only_wrong_samples,
    )

    task_data = json.loads(Path(args.task_json).read_text())
    examples = task_data["examples"]

    wait_for_server(args.base_url, timeout_sec=1800)
    client = VLLMChatClient(
        base_url=args.base_url,
        model=args.model,
        timeout_sec=900,
        seed=args.verifier_seed,
    )

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_json = out_dir / f"{args.out_prefix}_{ts}.json"
    out_md = out_dir / f"{args.out_prefix}_{ts}.md"

    report: dict[str, Any] = {
        "meta": {
            "model": args.model,
            "verifier_seed": args.verifier_seed,
            "base_url": args.base_url,
            "num_samples": args.num_samples,
            "seed": args.seed,
            "max_steps_per_trace": args.max_steps_per_trace,
            "merge_min_tokens": args.merge_min_tokens,
            "only_wrong_samples": args.only_wrong_samples,
            "context_window": args.context_window,
            "max_react_turns": args.max_react_turns,
            "engine_validation": validation,
            "source_predictions": str(args.predictions_jsonl),
        },
        "samples": [],
    }

    for sample_i, row in enumerate(selected, start=1):
        idx = int(row["index"])
        question = examples[idx]["input"]
        target = int(examples[idx]["target"])
        trace = row["response"]

        engine = MultiStepArithmeticEngine.from_question(question)
        truth = engine.compute_named_values()
        if truth["FINAL"] != target:
            raise RuntimeError(f"Engine mismatch at sample index {idx}: {truth['FINAL']} vs {target}")

        verifier = MultiStepReActStepVerifier(engine=engine, client=client, max_turns=args.max_react_turns)
        raw_steps = verifier.split_trace_steps(trace)
        merged_steps = merge_short_steps(raw_steps, args.merge_min_tokens)
        selected_indices = selected_step_indices(merged_steps, args.max_steps_per_trace)

        step_results = []
        for selected_pos, step_idx in enumerate(selected_indices, start=1):
            step_text = merged_steps[step_idx]
            prev_text, next_text = build_context_step_text(
                merged_steps,
                step_idx,
                args.context_window,
            )
            result = verifier.verify_step(
                question=question,
                step_text=step_text,
                step_id=selected_pos,
                prev_step_text=prev_text,
                next_step_text=next_text,
            )
            result["source_step_index"] = step_idx + 1
            step_results.append(result)
            print(
                f"sample {sample_i}/{args.num_samples} | idx={idx} | step {selected_pos}/{len(selected_indices)} (source={step_idx+1}) | verdict={result['verdict']}",
                flush=True,
            )

        counter = Counter(r["verdict"] for r in step_results)
        sample_report = {
            "sample_order": sample_i,
            "dataset_index": idx,
            "target": target,
            "computed_final": truth["FINAL"],
            "trace_char_len": len(trace),
            "raw_step_count": len(raw_steps),
            "merged_step_count": len(merged_steps),
            "selected_step_indices_1based": [i + 1 for i in selected_indices],
            "num_steps_verified": len(step_results),
            "verdict_distribution": dict(counter),
            "steps": step_results,
        }
        report["samples"].append(sample_report)

    overall = Counter()
    total_steps = 0
    for sample in report["samples"]:
        total_steps += sample["num_steps_verified"]
        overall.update(sample["verdict_distribution"])

    report["overall"] = {
        "total_steps_verified": total_steps,
        "verdict_distribution": dict(overall),
    }

    out_json.write_text(json.dumps(report, indent=2, ensure_ascii=False))

    md_lines = []
    md_lines.append("# Multi-step Arithmetic Step-level Verification (5 samples)")
    md_lines.append("")
    md_lines.append(f"- Model verifier: `{args.model}`")
    md_lines.append(f"- Total verified steps: `{total_steps}`")
    md_lines.append(f"- Verdict distribution: `{dict(overall)}`")
    md_lines.append("")

    for sample in report["samples"]:
        md_lines.append(f"## Sample idx `{sample['dataset_index']}`")
        md_lines.append(
            f"- Raw steps: `{sample['raw_step_count']}` | Merged steps: `{sample['merged_step_count']}` | Steps verified: `{sample['num_steps_verified']}`"
        )
        md_lines.append(
            f"- Distribution: `{sample['verdict_distribution']}`"
        )
        md_lines.append(f"- Computed final: `{sample['computed_final']}` | Target: `{sample['target']}`")
        md_lines.append("- First 8 step verdicts:")
        for step in sample["steps"][:8]:
            reason = step.get("reason", "").replace("\n", " ")
            md_lines.append(f"  - Step {step['step_id']}: `{step['verdict']}` — {reason[:140]}")
        md_lines.append("")

    out_md.write_text("\n".join(md_lines), encoding="utf-8")

    print(f"WROTE_JSON: {out_json}")
    print(f"WROTE_MD: {out_md}")


if __name__ == "__main__":
    main()
