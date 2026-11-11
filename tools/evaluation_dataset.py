"""Ground-truth dataset loading and leakage-safe grouped splitting."""
from __future__ import annotations

import csv
import json
import math
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

FEATURE_NAMES = (
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
)
REQUIRED_COLUMNS = frozenset((*FEATURE_NAMES, "label", "group_id"))


class DatasetFormatError(ValueError):
    """Raised when an evaluation dataset violates the required schema."""


@dataclass(frozen=True)
class DatasetRecord:
    """One labeled telemetry observation and its leakage boundary."""

    features: tuple[float, ...]
    label: int
    group_id: str


def _parse_feature(value: Any, name: str, row_number: int) -> float:
    if isinstance(value, bool):
        raise DatasetFormatError(f"row {row_number}: feature {name!r} must be numeric")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise DatasetFormatError(
            f"row {row_number}: feature {name!r} must be numeric"
        ) from exc
    if not math.isfinite(result):
        raise DatasetFormatError(
            f"row {row_number}: feature {name!r} must be finite"
        )
    return result


def _parse_label(value: Any, row_number: int) -> int:
    if isinstance(value, bool):
        raise DatasetFormatError(f"row {row_number}: label must be 0 or 1")
    try:
        label = int(value)
    except (TypeError, ValueError) as exc:
        raise DatasetFormatError(f"row {row_number}: label must be 0 or 1") from exc
    if str(value).strip() not in {"0", "1"} or label not in (0, 1):
        raise DatasetFormatError(f"row {row_number}: label must be 0 or 1")
    return label


def _record_from_mapping(mapping: dict[str, Any], row_number: int) -> DatasetRecord:
    missing = sorted(REQUIRED_COLUMNS - mapping.keys())
    if missing:
        raise DatasetFormatError(
            f"dataset row {row_number}: missing required columns: {', '.join(missing)}"
        )
    group_id = str(mapping["group_id"]).strip()
    if not group_id:
        raise DatasetFormatError(f"row {row_number}: group_id must not be empty")
    features = tuple(
        _parse_feature(mapping[name], name, row_number) for name in FEATURE_NAMES
    )
    return DatasetRecord(
        features=features,
        label=_parse_label(mapping["label"], row_number),
        group_id=group_id,
    )


def _load_csv(path: Path) -> list[DatasetRecord]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise DatasetFormatError("CSV dataset must have a header")
        missing = sorted(REQUIRED_COLUMNS - set(reader.fieldnames))
        if missing:
            raise DatasetFormatError(
                f"dataset header missing required columns: {', '.join(missing)}"
            )
        return [
            _record_from_mapping(row, row_number)
            for row_number, row in enumerate(reader, start=2)
        ]


def _load_jsonl(path: Path) -> list[DatasetRecord]:
    records: list[DatasetRecord] = []
    with path.open("r", encoding="utf-8") as handle:
        for row_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                mapping = json.loads(line)
            except json.JSONDecodeError as exc:
                raise DatasetFormatError(
                    f"row {row_number}: invalid JSON"
                ) from exc
            if not isinstance(mapping, dict):
                raise DatasetFormatError(f"row {row_number}: expected JSON object")
            records.append(_record_from_mapping(mapping, row_number))
    return records


def load_dataset(path: str | Path) -> list[DatasetRecord]:
    """Load CSV or JSONL labeled telemetry with mandatory group boundaries."""
    source = Path(path)
    if not source.is_file():
        raise DatasetFormatError(f"dataset does not exist: {source}")
    if source.suffix.lower() == ".csv":
        records = _load_csv(source)
    elif source.suffix.lower() in {".jsonl", ".ndjson"}:
        records = _load_jsonl(source)
    else:
        raise DatasetFormatError("dataset format must be .csv, .jsonl, or .ndjson")
    if not records:
        raise DatasetFormatError("dataset must contain at least one record")
    return records


def split_by_group(
    records: Iterable[DatasetRecord],
    *,
    seed: int = 42,
    proportions: tuple[float, float, float] = (0.6, 0.2, 0.2),
) -> tuple[list[DatasetRecord], list[DatasetRecord], list[DatasetRecord]]:
    """Split records without allowing a group to cross train/validation/test."""
    records = list(records)
    if not records:
        raise DatasetFormatError("cannot split an empty dataset")
    if len(proportions) != 3 or any(value <= 0 for value in proportions):
        raise DatasetFormatError("proportions must contain three positive values")
    total = sum(proportions)
    if not math.isclose(total, 1.0, rel_tol=0.0, abs_tol=1e-9):
        raise DatasetFormatError("proportions must sum to 1.0")

    grouped: dict[str, list[DatasetRecord]] = {}
    for record in records:
        grouped.setdefault(record.group_id, []).append(record)
    groups = list(grouped.items())
    random.Random(seed).shuffle(groups)

    targets = [len(records) * proportion for proportion in proportions]
    class_totals = {
        label: sum(record.label == label for record in records) for label in (0, 1)
    }
    class_targets = {
        label: [class_totals[label] * proportion for proportion in proportions]
        for label in (0, 1)
    }
    splits: list[list[DatasetRecord]] = [[], [], []]

    # Seed each partition with groups covering any class it still lacks when
    # the available group boundaries make that possible. This prevents a
    # valid dataset from producing a validation/test partition containing only
    # one class merely because group order was random.
    remaining = list(groups)
    for split_index in range(3):
        while remaining:
            present = {record.label for record in splits[split_index]}
            missing = {0, 1} - present
            if not missing:
                break
            candidate_index = max(
                range(len(remaining)),
                key=lambda index: (
                    len({record.label for record in remaining[index][1]} & missing),
                    -len(remaining[index][1]),
                    -index,
                ),
            )
            _, group_records = remaining.pop(candidate_index)
            splits[split_index].extend(group_records)

    for _, group_records in remaining:
        group_counts = {
            label: sum(record.label == label for record in group_records)
            for label in (0, 1)
        }

        def assignment_cost(candidate: int) -> tuple[float, float, int, int]:
            next_total = len(splits[candidate]) + len(group_records)
            target_ratio = next_total / targets[candidate]
            class_gap = sum(
                abs(
                    sum(record.label == label for record in splits[candidate])
                    + group_counts[label]
                    - class_targets[label][candidate]
                )
                for label in (0, 1)
            )
            return (target_ratio, class_gap, len(splits[candidate]), candidate)

        index = min(
            range(3),
            key=assignment_cost,
        )
        splits[index].extend(group_records)
    return splits[0], splits[1], splits[2]
