#!/usr/bin/env python3
"""Evaluate a frozen supervised MLP candidate on validation and holdout."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.evaluation_dataset import load_dataset
from tools.evaluate_socsoar_model import _load_model, normalize_for_model
from tools.model_evaluation import tune_threshold_frozen_model
from tools.phase4_heldout import evaluate_heldout


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(65536), b''):
            digest.update(chunk)
    return digest.hexdigest()


def evaluate_candidate(
    model_path: Path,
    train_path: Path,
    validation_path: Path,
    test_path: Path,
    output_path: Path,
    *,
    minimum_accuracy: float = 0.94,
) -> dict:
    train = load_dataset(train_path)
    validation = load_dataset(validation_path)
    test = load_dataset(test_path)
    model = _load_model(model_path)
    thresholds = tuple(value / 100.0 for value in range(50, 96, 5))
    threshold_selection = tune_threshold_frozen_model(
        model,
        validation,
        thresholds=thresholds,
        preprocess=normalize_for_model,
    )
    selected_threshold = threshold_selection['threshold']
    holdout = evaluate_heldout(
        train,
        validation,
        test,
        model=model,
        threshold=selected_threshold,
        minimum_accuracy=minimum_accuracy,
        preprocess=normalize_for_model,
    )
    report = {
        'status': holdout['status'],
        'model_fit': False,
        'model': {
            'path': str(model_path.resolve()),
            'sha256': sha256(model_path),
            'class': f'{type(model).__module__}.{type(model).__name__}',
        },
        'datasets': {
            'train': {'path': str(train_path.resolve()), 'sha256': sha256(train_path), 'rows': len(train)},
            'validation': {'path': str(validation_path.resolve()), 'sha256': sha256(validation_path), 'rows': len(validation)},
            'test': {'path': str(test_path.resolve()), 'sha256': sha256(test_path), 'rows': len(test)},
        },
        'threshold_source': 'validation',
        'threshold_selection': threshold_selection,
        'holdout': holdout,
        'quality_gate': holdout['quality_gate'],
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', type=Path, required=True)
    parser.add_argument('--train', type=Path, required=True)
    parser.add_argument('--validation', type=Path, required=True)
    parser.add_argument('--test', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--minimum-accuracy', type=float, default=0.94)
    args = parser.parse_args()
    report = evaluate_candidate(
        args.model,
        args.train,
        args.validation,
        args.test,
        args.output,
        minimum_accuracy=args.minimum_accuracy,
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report['quality_gate']['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
