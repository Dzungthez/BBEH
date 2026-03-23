"""
Build merged-step dataset for binary search verification.

Source:
  - splitter_dataset/deepseek/{split}.json  — raw steps + query/response/ground_truth
  - first_predictable/data/deepseek_merged/{split}.json — merge indices + annotations

Output (data/{split}.json): list of dicts with fields:
  id, subset, model, query, response, ground_truth, answer,
  steps (merged, with text), trace_label,
  first_wrong_step_index_1based (None for correct traces),
  first_predictable_merged_idx
"""

from __future__ import annotations

import json
from pathlib import Path

SPLITTER_DIR = Path(
    "/prj/corp/airesearch/lasvegas/vol11-scratch/huudnguy/code"
    "/step-hallucination/dataset/processed/splitter_dataset/deepseek"
)
MERGED_DIR = Path(
    "/prj/corp/airesearch/lasvegas/vol11-scratch/huudnguy/code"
    "/step-hallucination/experiments/first_predictable/data/deepseek_merged"
)
OUT_DIR = Path(__file__).parent / "data"


def load_splitter_by_id(split: str) -> dict[int, dict]:
    path = SPLITTER_DIR / f"{split}.json"
    data = json.loads(path.read_text())
    return {item["id"]: item for item in data}


def build_merged_steps(splitter_item: dict, merged_item: dict) -> list[dict]:
    """Merge fine-grained steps using step_ids from merged annotation."""
    raw_steps = splitter_item["steps"]  # list indexed by step_id
    raw_by_id = {s["step_id"]: s for s in raw_steps}

    merged_steps = []
    for ms in merged_item["steps"]:
        step_ids = ms["step_ids"]
        texts = [raw_by_id[sid]["text"] for sid in step_ids if sid in raw_by_id]
        merged_text = "\n".join(texts)
        merged_steps.append(
            {
                "merged_idx": ms["merged_idx"],
                "step_ids": step_ids,
                "text": merged_text,
                "step_hallucination": ms["step_hallucination"],
                "cumulative_hallucination": ms["cumulative_hallucination"],
                "first_predictable": ms["first_predictable"],
            }
        )
    return merged_steps


def build_split(split: str, splitter_by_id: dict[int, dict]) -> list[dict]:
    merged_path = MERGED_DIR / f"{split}.json"
    merged_data = json.loads(merged_path.read_text())

    out = []
    skipped = 0
    for m in merged_data:
        sid = m["id"]
        s = splitter_by_id.get(sid)
        if s is None:
            skipped += 1
            continue

        merged_steps = build_merged_steps(s, m)

        # first_wrong_step_index_1based: 1-based index of first step_hallucination=True
        # Use first_predictable_merged_idx when available (human annotation of
        # "first predictable wrong step"), else fall back to first step_hallucination.
        fp_idx = m.get("first_predictable_merged_idx")  # 0-based merged_idx or None
        if m["trace_label"] == 1:
            if fp_idx is not None:
                first_wrong_1based = fp_idx + 1
            else:
                # fallback: first step with step_hallucination=True
                hallu = [ms for ms in merged_steps if ms["step_hallucination"]]
                first_wrong_1based = hallu[0]["merged_idx"] + 1 if hallu else None
        else:
            first_wrong_1based = None  # correct trace

        out.append(
            {
                "id": sid,
                "subset": s.get("subset", ""),
                "model": s.get("model", ""),
                "query": s["query"],
                "response": s["response"],
                "answer": s.get("answer", ""),
                "ground_truth": s["ground_truth"],
                "trace_label": m["trace_label"],
                "n_merged_steps": m["n_merged_steps"],
                "first_predictable_merged_idx": fp_idx,
                "first_wrong_step_index_1based": first_wrong_1based,
                "steps": merged_steps,
            }
        )

    print(f"  {split}: {len(out)} built, {skipped} skipped (id not in splitter)")
    return out


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # Load all splitter splits into one lookup (ids are unique across splits)
    splitter_by_id: dict[int, dict] = {}
    for split in ("train", "val", "test"):
        splitter_by_id.update(load_splitter_by_id(split))
    print(f"Loaded {len(splitter_by_id)} splitter items total")

    for split in ("train", "val", "test"):
        print(f"Building {split}...")
        out = build_split(split, splitter_by_id)
        out_path = OUT_DIR / f"{split}.json"
        out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False))
        print(f"  -> {out_path}")

        # Quick stats
        wrong = [x for x in out if x["trace_label"] == 1]
        correct = [x for x in out if x["trace_label"] == 0]
        print(f"     wrong={len(wrong)}, correct={len(correct)}")


if __name__ == "__main__":
    main()
