#!/usr/bin/env python3
"""Train a reproducible SOC/SOAR candidate from reviewed normal groups."""
from __future__ import annotations

import argparse
import hashlib
import json
import pickle
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.evaluation_dataset import DatasetRecord, load_dataset, split_by_group
from tools.evaluate_socsoar_model import normalize_for_model


Preprocess = Callable[[Sequence[float]], Sequence[float]]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_revision() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def _load_template(path: Path) -> Any:
    model_dir = str(path.parent.resolve())
    if model_dir not in sys.path:
        sys.path.insert(0, model_dir)
    with path.open("rb") as handle:
        return pickle.load(handle)


def fit_candidate(
    model: Any,
    training_records: Sequence[DatasetRecord],
    *,
    preprocess: Preprocess = normalize_for_model,
) -> Any:
    """Fit only on normal records using the versioned runtime transform."""
    normal_rows = [
        list(preprocess(record.features))
        for record in training_records
        if record.label == 0
    ]
    if not normal_rows:
        raise ValueError("training data must contain at least one normal record")
    model.fit(normal_rows)
    return model


def train_candidate(
    dataset_path: Path,
    template_model_path: Path,
    output_model_path: Path,
    output_report_path: Path,
    *,
    seed: int = 42,
) -> dict[str, Any]:
    records = load_dataset(dataset_path)
    train, validation, test = split_by_group(records, seed=seed)
    model = fit_candidate(_load_template(template_model_path), train)

    output_model_path.parent.mkdir(parents=True, exist_ok=True)
    with output_model_path.open("wb") as handle:
        pickle.dump(model, handle, protocol=pickle.HIGHEST_PROTOCOL)

    report = {
        "status": "TRAINED",
        "source_revision": _git_revision(),
        "dataset": {
            "path": str(dataset_path.resolve()),
            "sha256": _sha256(dataset_path),
            "rows": len(records),
        },
        "template_model": {
            "path": str(template_model_path.resolve()),
            "sha256": _sha256(template_model_path),
        },
        "candidate_model": {
            "path": str(output_model_path.resolve()),
            "sha256": _sha256(output_model_path),
        },
        "training": {
            "fit_on": "normal_records_only",
            "preprocess": "normalize_for_model",
            "train_rows": len(train),
            "normal_train_rows": sum(record.label == 0 for record in train),
            "train_groups": sorted({record.group_id for record in train}),
        },
        "held_out": {
            "validation_rows": len(validation),
            "test_rows": len(test),
            "validation_groups": sorted({record.group_id for record in validation}),
            "test_groups": sorted({record.group_id for record in test}),
            "evaluation_required": True,
        },
        "quality_gate": {
            "model_quality_accepted": False,
            "reason": "candidate must pass frozen validation-only threshold and untouched holdout evaluation",
        },
    }
    output_report_path.parent.mkdir(parents=True, exist_ok=True)
    output_report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--template-model", type=Path, required=True)
    parser.add_argument("--output-model", type=Path, required=True)
    parser.add_argument("--output-report", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    report = train_candidate(
        args.dataset,
        args.template_model,
        args.output_model,
        args.output_report,
        seed=args.seed,
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
