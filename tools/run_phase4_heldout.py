#!/usr/bin/env python3
"""Run the final frozen-threshold held-out LightML evaluation gate."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Make the repository root importable when invoked directly.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.evaluation_dataset import load_dataset, split_by_group
from tools.evaluate_socsoar_model import _git_revision, _load_model, _sha256_file, normalize_for_model
from tools.model_evaluation import tune_threshold_frozen_model
from tools.phase4_heldout import HeldoutDatasetError, evaluate_heldout


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument(
        "--model",
        type=Path,
        default=Path("bundles/socsoar-alert-scoring/models/model.pkl"),
    )
    parser.add_argument("--threshold", type=float, default=0.75)
    parser.add_argument("--tune-threshold-on-validation", action="store_true")
    parser.add_argument(
        "--threshold-grid",
        default="0.50,0.55,0.60,0.65,0.70,0.75,0.80,0.85,0.90,0.95",
    )
    parser.add_argument("--minimum-accuracy", type=float, default=0.94)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    records = load_dataset(args.dataset)
    train, validation, test = split_by_group(records, seed=args.seed)
    threshold = args.threshold
    threshold_source = "explicit"
    threshold_tuning = None
    if args.tune_threshold_on_validation:
        grid = tuple(
            float(value.strip())
            for value in args.threshold_grid.split(",")
            if value.strip()
        )
        threshold_tuning = tune_threshold_frozen_model(
            _load_model(args.model),
            validation,
            thresholds=grid,
            preprocess=normalize_for_model,
        )
        threshold = threshold_tuning["threshold"]
        threshold_source = "validation"

    try:
        report = evaluate_heldout(
            train,
            validation,
            test,
            model=_load_model(args.model),
            threshold=threshold,
            minimum_accuracy=args.minimum_accuracy,
            preprocess=normalize_for_model,
        )
    except HeldoutDatasetError as exc:
        report = {
            "phase": 4,
            "status": "INVALID_DATASET",
            "error": str(exc),
            "quality_gate": {"passed": False},
        }
        rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
        if args.output:
            args.output.write_text(rendered, encoding="utf-8")
        print(rendered, end="")
        return 2

    report.update(
        {
            "source_revision": _git_revision(Path(__file__).resolve().parents[1]),
            "dataset": {
                "path": str(args.dataset.resolve()),
                "sha256": _sha256_file(args.dataset),
                "rows": len(records),
            },
            "model": {
                "path": str(args.model.resolve()),
                "sha256": _sha256_file(args.model),
            },
            "feature_order": [
                "pid_ratio",
                "ppid_ratio",
                "cmd_len",
                "arg_count",
                "token_entropy",
                "uid_class",
                "start_tick_delta",
                "mem_rss_ratio",
                "cpu_tick_ratio",
                "fd_count",
                "thread_count",
                "privilege_flag",
            ],
            "seed": args.seed,
            "threshold_source": threshold_source,
            "threshold_tuning": threshold_tuning,
            "split": {
                "train_rows": len(train),
                "validation_rows": len(validation),
                "test_rows": len(test),
                "train_groups": sorted({record.group_id for record in train}),
                "validation_groups": sorted({record.group_id for record in validation}),
                "test_groups": sorted({record.group_id for record in test}),
            },
        }
    )
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if report["quality_gate"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
