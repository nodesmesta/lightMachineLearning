"""Strict final held-out evaluation gate for LightML quality claims."""
from __future__ import annotations

from typing import Any, Iterable

from tools.evaluation_dataset import DatasetRecord
from tools.model_evaluation import evaluate_isolation_forest


class HeldoutDatasetError(ValueError):
    """Raised when a Phase 4 evaluation boundary is invalid."""


def _groups(records: Iterable[DatasetRecord]) -> set[str]:
    return {record.group_id for record in records}


def _labels(records: Iterable[DatasetRecord]) -> set[int]:
    return {record.label for record in records}


def _validate_partitions(
    train: list[DatasetRecord],
    validation: list[DatasetRecord],
    test: list[DatasetRecord],
) -> None:
    if not train or not validation or not test:
        raise HeldoutDatasetError("train, validation, and test partitions must be non-empty")
    partitions = {"train": _groups(train), "validation": _groups(validation), "test": _groups(test)}
    names = tuple(partitions)
    for index, left_name in enumerate(names):
        for right_name in names[index + 1 :]:
            overlap = partitions[left_name] & partitions[right_name]
            if overlap:
                raise HeldoutDatasetError(
                    f"group overlap between {left_name} and {right_name}: {sorted(overlap)}"
                )
    if _labels(test) != {0, 1}:
        raise HeldoutDatasetError("held-out test partition must contain both labels 0 and 1")
    if 0 not in _labels(train):
        raise HeldoutDatasetError("training partition must contain normal records")


def evaluate_heldout(
    train_records: Iterable[DatasetRecord],
    validation_records: Iterable[DatasetRecord],
    test_records: Iterable[DatasetRecord],
    *,
    model: Any,
    threshold: float,
    minimum_accuracy: float = 0.94,
) -> dict[str, Any]:
    """Evaluate a frozen threshold once on a validated held-out partition."""
    train = list(train_records)
    validation = list(validation_records)
    test = list(test_records)
    _validate_partitions(train, validation, test)
    if not 0.0 <= threshold <= 1.0:
        raise HeldoutDatasetError("frozen threshold must be between 0.0 and 1.0")

    report = evaluate_isolation_forest(model, train, test, threshold=threshold)
    passed = report["accuracy"] >= minimum_accuracy
    return {
        "phase": 4,
        "status": "PASS" if passed else "FAIL",
        "selection_set": "frozen",
        "threshold": threshold,
        "test": report,
        "quality_gate": {
            "metric": "test.accuracy",
            "minimum": minimum_accuracy,
            "actual": report["accuracy"],
            "passed": passed,
        },
    }
