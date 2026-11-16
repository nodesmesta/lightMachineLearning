#!/usr/bin/env python3
"""Train one dependency-free supervised LightML candidate."""
from __future__ import annotations

import argparse
import hashlib
import json
import pickle
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
MODEL_DIR = REPO_ROOT / 'bundles' / 'socsoar-alert-scoring' / 'models'
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(MODEL_DIR) not in sys.path:
    sys.path.insert(0, str(MODEL_DIR))

from tools.evaluation_dataset import DatasetRecord, load_dataset
from tools.evaluate_socsoar_model import normalize_for_model
from supervised_mlp import SupervisedMLP


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(65536), b''):
            digest.update(chunk)
    return digest.hexdigest()


def revision() -> str:
    result = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=REPO_ROOT, capture_output=True, text=True, check=False)
    return result.stdout.strip() if result.returncode == 0 else 'unknown'


def fit_candidate(
    records: Sequence[DatasetRecord],
    *,
    seed: int = 42,
    epochs: int = 400,
    learning_rate: float = 0.03,
    l2: float = 1e-4,
    hidden_dims: tuple[int, int] = (32, 16),
) -> SupervisedMLP:
    rows = [list(normalize_for_model(record.features)) for record in records]
    labels = [record.label for record in records]
    model = SupervisedMLP(
        seed=seed,
        epochs=epochs,
        learning_rate=learning_rate,
        l2=l2,
        hidden_dims=hidden_dims,
    )
    model.fit(rows, labels)
    return model


def train_and_save(
    train_path: Path,
    output_model: Path,
    output_report: Path,
    *,
    seed: int = 42,
    epochs: int = 400,
    learning_rate: float = 0.03,
    l2: float = 1e-4,
) -> dict[str, Any]:
    records = load_dataset(train_path)
    model = fit_candidate(
        records,
        seed=seed,
        epochs=epochs,
        learning_rate=learning_rate,
        l2=l2,
    )
    output_model.parent.mkdir(parents=True, exist_ok=True)
    with output_model.open('wb') as handle:
        pickle.dump(model, handle, protocol=pickle.HIGHEST_PROTOCOL)
    report = {
        'status': 'TRAINED',
        'source_revision': revision(),
        'dataset': {
            'path': str(train_path.resolve()),
            'sha256': sha256(train_path),
            'rows': len(records),
            'groups': sorted({record.group_id for record in records}),
        },
        'model': {
            'path': str(output_model.resolve()),
            'sha256': sha256(output_model),
            'class': 'supervised_mlp.SupervisedMLP',
        },
        'training': {
            'fit_on': 'reviewed train partition labels 0 and 1',
            'preprocess': 'normalize_for_model followed by training-partition standardization',
            'seed': seed,
            'epochs_configured': epochs,
            'epochs_realized': model.training_epochs_,
            'learning_rate': learning_rate,
            'l2': l2,
            'hidden_dims': list(model.hidden_dims),
            'feature_order': model.feature_names_in_,
        },
        'quality_gate': {
            'model_quality_accepted': False,
            'reason': 'candidate requires validation-only selection and frozen untouched holdout evaluation',
        },
    }
    output_report.parent.mkdir(parents=True, exist_ok=True)
    output_report.write_text(json.dumps(report, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--train', type=Path, required=True)
    parser.add_argument('--output-model', type=Path, required=True)
    parser.add_argument('--output-report', type=Path, required=True)
    parser.add_argument('--seed', type=int, default=42)
    parser.add_argument('--epochs', type=int, default=400)
    parser.add_argument('--learning-rate', type=float, default=0.03)
    parser.add_argument('--l2', type=float, default=1e-4)
    args = parser.parse_args()
    report = train_and_save(
        args.train,
        args.output_model,
        args.output_report,
        seed=args.seed,
        epochs=args.epochs,
        learning_rate=args.learning_rate,
        l2=args.l2,
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
