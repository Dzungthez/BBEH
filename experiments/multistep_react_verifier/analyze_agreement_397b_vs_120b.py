"""
Compute within-model agreement (5 seeds) for 397B nightly runs,
then cross-model agreement between 397B ensemble and 120B ensemble.

Usage:
    python -m experiments.multistep_react_verifier.analyze_agreement_397b_vs_120b \
        --runs-397b experiments/multistep_react_verifier/logs/397b_nightly_50x5/verify_50samples_397b_nightly_seed*.json \
        --runs-120b experiments/multistep_react_verifier/logs/ensemble_runs_50x7/verify_5samples_*.json \
        --out-dir experiments/multistep_react_verifier/logs/397b_nightly_50x5
"""
from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any


def load_run(path: str) -> dict[str, Any]:
    return json.loads(Path(path).read_text())


def build_step_map(run: dict[str, Any]) -> dict[tuple[int, int], str]:
    """Returns {(dataset_index, step_id): verdict}."""
    result = {}
    for sample in run["samples"]:
        idx = sample["dataset_index"]
        for step in sample["steps"]:
            key = (idx, step["step_id"])
            result[key] = step["verdict"]
    return result


def majority_verdict(verdicts: list[str]) -> str:
    c = Counter(verdicts)
    return c.most_common(1)[0][0]


def compute_agreement(
    runs: list[dict[str, Any]],
    label: str,
) -> dict[str, Any]:
    step_maps = [build_step_map(r) for r in runs]
    common_keys = set(step_maps[0].keys())
    for sm in step_maps[1:]:
        common_keys &= set(sm.keys())
    common_keys = sorted(common_keys)

    incomplete = sum(1 for k in common_keys if any(k not in sm for sm in step_maps))

    unanimous = 0
    majority_fracs = []
    ensemble_verdicts: dict[tuple[int, int], str] = {}
    overall_counter: Counter = Counter()

    for k in common_keys:
        vs = [sm[k] for sm in step_maps]
        c = Counter(vs)
        top_count = c.most_common(1)[0][1]
        frac = top_count / len(vs)
        majority_fracs.append(frac)
        if top_count == len(vs):
            unanimous += 1
        mv = majority_verdict(vs)
        ensemble_verdicts[k] = mv
        overall_counter[mv] += 1

    run_dists = [dict(Counter(sm[k] for k in common_keys)) for sm in step_maps]

    return {
        "label": label,
        "num_runs": len(runs),
        "num_steps_common": len(common_keys),
        "incomplete_keys": incomplete,
        "unanimous_steps": unanimous,
        "unanimous_rate": unanimous / len(common_keys) if common_keys else 0.0,
        "mean_majority_fraction": sum(majority_fracs) / len(majority_fracs) if majority_fracs else 0.0,
        "ensemble_majority_distribution": dict(overall_counter),
        "run_verdict_distribution": run_dists,
        "ensemble_verdicts": ensemble_verdicts,
        "common_keys": common_keys,
    }


def cross_model_agreement(
    ens_a: dict[str, Any],
    ens_b: dict[str, Any],
    label_a: str,
    label_b: str,
) -> dict[str, Any]:
    """Compare majority verdicts of two ensembles step-by-step."""
    keys_a = set(ens_a["ensemble_verdicts"].keys())
    keys_b = set(ens_b["ensemble_verdicts"].keys())
    common = sorted(keys_a & keys_b)

    agree = 0
    disagree_rows = []
    confusion: dict[str, dict[str, int]] = {}

    for k in common:
        va = ens_a["ensemble_verdicts"][k]
        vb = ens_b["ensemble_verdicts"][k]
        confusion.setdefault(va, {}).setdefault(vb, 0)
        confusion[va][vb] += 1
        if va == vb:
            agree += 1
        else:
            disagree_rows.append({"dataset_index": k[0], "step_id": k[1], label_a: va, label_b: vb})

    agree_rate = agree / len(common) if common else 0.0

    return {
        "label_a": label_a,
        "label_b": label_b,
        "num_steps_common": len(common),
        "agree_steps": agree,
        "agree_rate": agree_rate,
        "confusion_matrix": confusion,
        "disagreements": sorted(disagree_rows, key=lambda r: (r["dataset_index"], r["step_id"])),
    }


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--runs-397b", nargs="+", required=True)
    p.add_argument("--runs-120b", nargs="+", required=True)
    p.add_argument("--out-dir", default="experiments/multistep_react_verifier/logs/397b_nightly_50x5")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    runs_397b = [load_run(p) for p in sorted(args.runs_397b)]
    runs_120b = [load_run(p) for p in sorted(args.runs_120b)]

    print(f"Loaded {len(runs_397b)} runs for 397B, {len(runs_120b)} runs for 120B")

    # Within-model agreement
    agr_397b = compute_agreement(runs_397b, "397B-nightly")
    agr_120b = compute_agreement(runs_120b, "120B")

    # Cross-model agreement
    cross = cross_model_agreement(agr_397b, agr_120b, "397B-nightly", "120B")

    # Print summary
    print("\n=== 397B within-model agreement ===")
    print(f"  Seeds: {agr_397b['num_runs']}, Steps: {agr_397b['num_steps_common']}")
    print(f"  Unanimous rate: {agr_397b['unanimous_rate']:.3f}")
    print(f"  Mean majority fraction: {agr_397b['mean_majority_fraction']:.3f}")
    print(f"  Ensemble distribution: {agr_397b['ensemble_majority_distribution']}")
    print(f"  Per-run distributions:")
    for i, d in enumerate(agr_397b['run_verdict_distribution'], 1):
        print(f"    seed {i}: {d}")

    print("\n=== 120B within-model agreement ===")
    print(f"  Seeds: {agr_120b['num_runs']}, Steps: {agr_120b['num_steps_common']}")
    print(f"  Unanimous rate: {agr_120b['unanimous_rate']:.3f}")
    print(f"  Mean majority fraction: {agr_120b['mean_majority_fraction']:.3f}")
    print(f"  Ensemble distribution: {agr_120b['ensemble_majority_distribution']}")

    print("\n=== Cross-model agreement (397B majority vs 120B majority) ===")
    print(f"  Common steps: {cross['num_steps_common']}")
    print(f"  Agree rate: {cross['agree_rate']:.3f} ({cross['agree_steps']}/{cross['num_steps_common']})")
    print(f"  Confusion matrix (rows=397B, cols=120B):")
    for va, row in sorted(cross['confusion_matrix'].items()):
        for vb, cnt in sorted(row.items()):
            print(f"    {va:10s} -> {vb:10s}: {cnt}")
    print(f"  Disagreements: {len(cross['disagreements'])}")

    # Save JSON
    report = {
        "agreement_397b": {k: v for k, v in agr_397b.items() if k not in ("ensemble_verdicts", "common_keys")},
        "agreement_120b": {k: v for k, v in agr_120b.items() if k not in ("ensemble_verdicts", "common_keys")},
        "cross_model": {k: v for k, v in cross.items() if k != "disagreements"},
        "cross_model_disagreements": cross["disagreements"],
    }
    out_json = out_dir / "agreement_397b_vs_120b.json"
    out_json.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"\nWROTE_JSON: {out_json}")

    # Save disagreements CSV
    out_csv = out_dir / "disagreements_397b_vs_120b.csv"
    if cross["disagreements"]:
        with open(out_csv, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["dataset_index", "step_id", "397B-nightly", "120B"])
            writer.writeheader()
            writer.writerows(cross["disagreements"])
        print(f"WROTE_CSV: {out_csv}")


if __name__ == "__main__":
    main()
