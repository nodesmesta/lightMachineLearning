#!/usr/bin/env python3
"""Evaluate the current SOC/SOAR model against a labeled dataset."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import pickle
import subprocess
import sys
from pathlib import Path
from typing import Any

# Make the repository root importable when this file is invoked directly.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.evaluation_dataset import load_dataset, split_by_group
from tools.model_evaluation import evaluate_isolation_forest, tune_threshold


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_revision(path: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=path,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def normalize_for_model(vec: list[float]) -> list[float]:
    """Match Project 3 bundle preprocessing before model evaluation."""
    if len(vec) != 12:
        raise ValueError(f"Expected exactly 12 features, got {len(vec)}")
    normalized = list(vec)
    normalized[2] = min(1.0, math.log1p(normalized[2]) / 10.0)
    normalized[3] = min(1.0, normalized[3] / 50.0)
    normalized[4] = min(1.0, normalized[4] / 8.0)
    normalized[6] = min(1.0, normalized[6] / 10000.0)
    normalized[9] = min(1.0, normalized[9] / 1024.0)
    normalized[10] = min(1.0, normalized[10] / 100.0)
    return [round(value, 4) for value in normalized]


def _load_model(path: Path) -> Any:
    model_dirs = [
        path.parent.resolve(),
        REPO_ROOT / "bundles" / "socsoar-alert-scoring" / "models",
    ]
    for model_dir_path in model_dirs:
        model_dir = str(model_dir_path)
        if model_dir not in sys.path:
            sys.path.insert(0, model_dir)
    with path.open("rb") as handle:
        return pickle.load(handle)


def build_report(
    dataset_path: Path,
    model_path: Path,
    train: list[Any],
    validation: list[Any],
    test: list[Any],
    *,
    threshold: float,
    minimum_accuracy: float,
    tune_threshold_enabled: bool = False,
    threshold_grid: tuple[float, ...] = (),
) -> dict[str, Any]:
    model_bytes = model_path.read_bytes()
    tuning = None
    if tune_threshold_enabled:
        tuning = tune_threshold(
            _load_model(model_path),
            train,
            validation,
            thresholds=threshold_grid,
            preprocess=normalize_for_model,
        )
        selected_threshold = tuning["threshold"]
        validation_report = tuning["report"]
    else:
        selected_threshold = threshold
        validation_report = evaluate_isolation_forest(
            _load_model(model_path), train, validation, threshold=selected_threshold,
            preprocess=normalize_for_model,
        )
    test_report = evaluate_isolation_forest(
        _load_model(model_path), train, test, threshold=selected_threshold,
        preprocess=normalize_for_model,
    )
    return {
        "source_revision": _git_revision(Path(__file__).resolve().parents[1]),
        "dataset": {
            "path": str(dataset_path.resolve()),
            "sha256": _sha256_file(dataset_path),
            "rows": len(train) + len(validation) + len(test),
        },
        "model": {
            "path": str(model_path.resolve()),
            "sha256": hashlib.sha256(model_bytes).hexdigest(),
        },
        "split": {
            "train_rows": len(train),
            "validation_rows": len(validation),
            "test_rows": len(test),
            "train_groups": sorted({record.group_id for record in train}),
            "validation_groups": sorted({record.group_id for record in validation}),
            "test_groups": sorted({record.group_id for record in test}),
        },
        "threshold": selected_threshold,
        "minimum_accuracy": minimum_accuracy,
        "threshold_tuning": tuning,
        "validation": validation_report,
        "test": test_report,
        "quality_gate": {
            "metric": "test.accuracy",
            "passed": test_report["accuracy"] >= minimum_accuracy,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument(
        "--model",
        type=Path,
        default=Path("bundles/socsoar-alert-scoring/models/model.pkl"),
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--threshold", type=float, default=0.75)
    parser.add_argument("--tune-threshold", action="store_true")
    parser.add_argument(
        "--threshold-grid",
        default="0.50,0.55,0.60,0.65,0.70,0.75,0.80,0.85,0.90,0.95",
    )
    parser.add_argument("--minimum-accuracy", type=float, default=0.94)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    records = load_dataset(args.dataset)
    train, validation, test = split_by_group(records, seed=args.seed)
    threshold_grid = tuple(
        float(value.strip()) for value in args.threshold_grid.split(",") if value.strip()
    )
    report = build_report(
        args.dataset,
        args.model,
        train,
        validation,
        test,
        threshold=args.threshold,
        minimum_accuracy=args.minimum_accuracy,
        tune_threshold_enabled=args.tune_threshold,
        threshold_grid=threshold_grid,
    )
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if report["quality_gate"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
