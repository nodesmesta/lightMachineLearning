# Phase 1 evaluation dataset contract

Phase 1 defines the input contract and leakage-safe split for future LightML quality evaluation. It does not claim model accuracy and it does not modify the production bundle.

## Required record fields

Every CSV row or JSONL object must contain:

- `pid_ratio`
- `ppid_ratio`
- `cmd_len`
- `arg_count`
- `token_entropy`
- `uid_class`
- `start_tick_delta`
- `mem_rss_ratio`
- `cpu_tick_ratio`
- `fd_count`
- `thread_count`
- `privilege_flag`
- `label` — `0` for normal, `1` for anomaly
- `group_id` — host/session/time-group boundary used to prevent leakage

Feature values must be finite numbers. Boolean, NaN, and infinity values are rejected. `group_id` must be non-empty.

## Supported formats

- CSV with a header row
- JSON Lines (`.jsonl` or `.ndjson`), one object per line

Example CSV header:

```text
pid_ratio,ppid_ratio,cmd_len,arg_count,token_entropy,uid_class,start_tick_delta,mem_rss_ratio,cpu_tick_ratio,fd_count,thread_count,privilege_flag,label,group_id
```

## Leakage-safe split

Use `tools.evaluation_dataset.split_by_group()` with a fixed seed. The splitter assigns complete groups to exactly one of:

- training: 60%
- validation: 20%
- held-out test: 20%

A group must never appear in more than one split. The split result is deterministic for the same input order, seed, and proportions.

Do not fall back to row-level random splitting when `group_id` is unavailable; that would allow host/session leakage and invalidate the accuracy measurement.

## Phase 2 baseline evaluation

Run the current model against a real labeled dataset with:

```sh
python3 tools/evaluate_socsoar_model.py \
  --dataset /absolute/path/to/telemetry.csv \
  --model bundles/socsoar-alert-scoring/models/model.pkl \
  --output /absolute/path/to/baseline-report.json
```

The runner fits the current Isolation Forest only on normal training records, evaluates validation and held-out test groups at the declared threshold, and exits non-zero when `test.accuracy` is below the default `0.94` gate. The JSON report records model and dataset hashes, source revision, split groups, confusion matrix, accuracy, balanced accuracy, precision, recall, F1, false-positive rate, and false-negative rate.

The smoke fixture used during development is not production evidence. A real labeled dataset is required before making an accuracy claim.

## Phase 3 validation-only threshold tuning

To select a threshold from validation data and then apply it once to the held-out test set:

```sh
python3 tools/evaluate_socsoar_model.py \
  --dataset /absolute/path/to/telemetry.csv \
  --tune-threshold \
  --threshold-grid 0.50,0.55,0.60,0.65,0.70,0.75,0.80,0.85,0.90,0.95 \
  --output /absolute/path/to/tuned-baseline-report.json
```

Threshold selection optimizes validation accuracy first, then balanced accuracy, F1, and recall. The test set is never passed to the tuning function. The selected threshold and every candidate result are retained in `threshold_tuning` in the JSON report.

## Phase 4 final held-out gate

Run the final evaluation with a frozen threshold, or explicitly tune on validation before freezing it:

```sh
python3 tools/run_phase4_heldout.py \
  --dataset /absolute/path/to/telemetry.csv \
  --tune-threshold-on-validation \
  --output /absolute/path/to/phase4-heldout-report.json
```

Phase 4 rejects empty partitions, overlapping groups, a test partition missing either class, a training partition without normal records, and thresholds outside `[0, 1]`. It returns exit code `0` only when held-out `test.accuracy >= 0.94`, `1` for a valid evaluation that fails the quality gate, and `2` for an invalid dataset boundary. It does not modify `model.pkl`.
