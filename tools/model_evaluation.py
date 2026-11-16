"""Baseline quality metrics for the current LightML anomaly scorer."""
from __future__ import annotations

from typing import Any, Callable, Iterable, Sequence

from tools.evaluation_dataset import DatasetRecord


def _rate(numerator: float, denominator: float) -> float:
    return numerator / denominator if denominator else 0.0


def evaluate_predictions(
    labels: Sequence[int],
    predictions: Sequence[int],
    scores: Sequence[float],
    threshold: float,
) -> dict[str, Any]:
    """Return binary classification metrics for normal=0 and anomaly=1."""
    if not (len(labels) == len(predictions) == len(scores)):
        raise ValueError("labels, predictions, and scores must have equal length")
    if not labels:
        raise ValueError("cannot evaluate an empty prediction set")
    if any(label not in (0, 1) for label in labels):
        raise ValueError("labels must contain only 0 or 1")
    if any(prediction not in (0, 1) for prediction in predictions):
        raise ValueError("predictions must contain only 0 or 1")

    tn = fp = fn = tp = 0
    for label, prediction in zip(labels, predictions):
        if label == 0 and prediction == 0:
            tn += 1
        elif label == 0 and prediction == 1:
            fp += 1
        elif label == 1 and prediction == 0:
            fn += 1
        else:
            tp += 1

    total = len(labels)
    tpr = _rate(tp, tp + fn)
    tnr = _rate(tn, tn + fp)
    precision = _rate(tp, tp + fp)
    recall = tpr
    f1 = _rate(2 * precision * recall, precision + recall)
    return {
        "sample_count": total,
        "class_counts": {
            "normal": labels.count(0),
            "anomaly": labels.count(1),
        },
        "threshold": float(threshold),
        "confusion_matrix": {"tn": tn, "fp": fp, "fn": fn, "tp": tp},
        "accuracy": _rate(tn + tp, total),
        "balanced_accuracy": (tpr + tnr) / 2.0,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "false_positive_rate": _rate(fp, fp + tn),
        "false_negative_rate": _rate(fn, fn + tp),
    }


def evaluate_isolation_forest(
    model: Any,
    train_records: Iterable[DatasetRecord],
    evaluation_records: Iterable[DatasetRecord],
    *,
    threshold: float = 0.75,
    preprocess: Callable[[Sequence[float]], Sequence[float]] | None = None,
) -> dict[str, Any]:
    """Fit the current unsupervised model on normal records and evaluate labels."""
    train_records = list(train_records)
    evaluation_records = list(evaluation_records)
    normal_rows = [list(record.features) for record in train_records if record.label == 0]
    if not normal_rows:
        raise ValueError("training set must contain at least one normal record")
    if not evaluation_records:
        raise ValueError("evaluation set must contain at least one record")

    transform = preprocess or (lambda row: row)
    model.fit([list(transform(row)) for row in normal_rows])
    return evaluate_frozen_model(
        model,
        evaluation_records,
        threshold=threshold,
        preprocess=preprocess,
    )


def evaluate_frozen_model(
    model: Any,
    evaluation_records: Iterable[DatasetRecord],
    *,
    threshold: float = 0.75,
    preprocess: Callable[[Sequence[float]], Sequence[float]] | None = None,
) -> dict[str, Any]:
    """Score records with an already-fitted model without mutating it."""
    evaluation_records = list(evaluation_records)
    if not evaluation_records:
        raise ValueError("evaluation set must contain at least one record")

    transform = preprocess or (lambda row: row)
    rows = [list(transform(record.features)) for record in evaluation_records]
    probabilities = model.predict_proba(rows)
    scores = [float(result[1]) for result in probabilities]
    predictions = [1 if score > threshold else 0 for score in scores]
    return evaluate_predictions(
        [record.label for record in evaluation_records],
        predictions,
        scores,
        threshold,
    )


def tune_threshold(
    model: Any,
    train_records: Iterable[DatasetRecord],
    validation_records: Iterable[DatasetRecord],
    *,
    thresholds: Iterable[float],
    preprocess: Callable[[Sequence[float]], Sequence[float]] | None = None,
) -> dict[str, Any]:
    """Select a threshold using validation metrics only.

    The model is fit once on normal training records. The held-out test set is
    intentionally not accepted by this function, preventing test-set tuning.
    """
    train_records = list(train_records)
    validation_records = list(validation_records)
    threshold_grid = sorted({float(value) for value in thresholds})
    if not threshold_grid:
        raise ValueError("threshold grid must contain at least one value")
    if any(value < 0.0 or value > 1.0 for value in threshold_grid):
        raise ValueError("thresholds must be between 0.0 and 1.0")
    normal_rows = [list(record.features) for record in train_records if record.label == 0]
    if not normal_rows:
        raise ValueError("training set must contain at least one normal record")
    if not validation_records:
        raise ValueError("validation set must contain at least one record")

    transform = preprocess or (lambda row: row)
    model.fit([list(transform(row)) for row in normal_rows])
    rows = [list(transform(record.features)) for record in validation_records]
    scores = [float(result[1]) for result in model.predict_proba(rows)]
    labels = [record.label for record in validation_records]
    candidates = []
    for threshold in threshold_grid:
        predictions = [1 if score > threshold else 0 for score in scores]
        report = evaluate_predictions(labels, predictions, scores, threshold)
        candidates.append(report)

    best = max(
        candidates,
        key=lambda report: (
            report["accuracy"],
            report["balanced_accuracy"],
            report["f1"],
            report["recall"],
            -report["threshold"],
        ),
    )
    return {
        "threshold": best["threshold"],
        "report": best,
        "candidates": candidates,
        "selection_set": "validation",
    }


def tune_threshold_frozen_model(
    model: Any,
    validation_records: Iterable[DatasetRecord],
    *,
    thresholds: Iterable[float],
    preprocess: Callable[[Sequence[float]], Sequence[float]] | None = None,
) -> dict[str, Any]:
    """Select a threshold from validation scores without fitting the model."""
    validation_records = list(validation_records)
    threshold_grid = sorted({float(value) for value in thresholds})
    if not threshold_grid:
        raise ValueError("threshold grid must contain at least one value")
    if any(value < 0.0 or value > 1.0 for value in threshold_grid):
        raise ValueError("thresholds must be between 0.0 and 1.0")
    if not validation_records:
        raise ValueError("validation set must contain at least one record")

    candidates = [
        evaluate_frozen_model(
            model,
            validation_records,
            threshold=threshold,
            preprocess=preprocess,
        )
        for threshold in threshold_grid
    ]
    best = max(
        candidates,
        key=lambda report: (
            report["accuracy"],
            report["balanced_accuracy"],
            report["f1"],
            report["recall"],
            -report["threshold"],
        ),
    )
    return {
        "threshold": best["threshold"],
        "report": best,
        "candidates": candidates,
        "selection_set": "validation",
        "model_fit": False,
    }
