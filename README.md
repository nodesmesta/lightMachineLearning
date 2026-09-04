# 04092026

## Summary

This report covers **PHASE 2 / DAY 4B - K5 SYSTEMD PRODUCTION-PATH REVISION**
for SYDECO LightML V2. The work is derived from the review document
`Jamaludin REPORTS 3 SEPTEMBER REVIEWED.docx`, which did not accept Day 4 K5
as closed.

Reviewer verdict for the 03-09 submission:

> I do not yet accept PHASE 2 / DAY 4 - K5 as closed.

Reviewer score: **84/100 - technically strong work, but REVISION REQUIRED
before K5 acceptance.**

The blocker was specific: K5 behavior was proven mostly on
`InProcessWorkerHost`, while the production-path `SystemdTransientWorkerHost`
did not yet have equivalent timeout, terminal-frame, lifecycle, cancellation,
and backpressure proof.

Status after this revision:

**PHASE 2 / DAY 4 - K5 STREAMING: REVISION SUBMITTED / AWAITING REVIEW.**

## Scope

This revision only closes K5 production-path parity. It does not start J4B,
CRA integration, F4/F6 schema migration/update handling, production signing
key infrastructure, permanent installer/unit acceptance, or LightML 1.0.1
changes.

## Commit Log

| Commit | Purpose |
|--------|---------|
| `5d6e8d5` | Close K5 systemd streaming parity: tests plus production-path fixes for P1-P5 |

Commit count after the implementation commit: **18**.

The final README/report/package commit is created after this README is written.
Final clean-tree state is recorded in the P8 evidence and package hash section.

## P0 - Baseline And Reviewer Findings

Baseline repository state before code changes:

```text
HEAD: b21eb0f
commit count: 17
git status: clean
```

Existing streaming tests were run from an isolated copy:

```text
python3 -m unittest discover -s tests -p 'test_streaming_systemd_auth.py' -v
Ran 2 tests in 1.014s
OK

python3 -m unittest discover -s tests -p 'test_streaming*.py' -v
Ran 25 tests in 141.727s
OK
```

Baseline findings confirmed:

| Finding | Baseline status |
|---------|-----------------|
| Systemd stream used ordinary `inference_timeout` as HTTP timeout | confirmed |
| `stream_first_chunk_timeout`, `stream_idle_timeout`, `stream_total_timeout` were not independently enforced in systemd path | confirmed |
| EOF meant normal completion without explicit terminal frame | confirmed |
| Worker runtime encoded stream errors as normal `data` | confirmed |
| Batch streaming silently used `items[0]` | confirmed |
| Existing systemd stream tests covered auth only | confirmed |

Evidence: `data/evidence_p0_systemd_k5_baseline.txt`.

## P1 - RED Systemd Parity Tests

Added `tests/test_streaming_systemd_parity.py` with focused RED coverage for
the production-path host class.

RED baseline:

```text
python3 -m unittest discover -s tests -p 'test_streaming_systemd_parity.py' -v
Ran 8 tests in 5.055s
FAILED (failures=7, errors=1)
```

The RED tests cover:

| Test area | Production-path requirement |
|-----------|-----------------------------|
| first chunk timeout | no first chunk beyond bound -> `StreamTimeout("first_chunk")` |
| idle timeout | stall between chunks -> `StreamTimeout("idle")` |
| total timeout | active stream cannot exceed monotonic total deadline |
| adapter error frame | internal error is not yielded as chunk data |
| abnormal EOF | EOF before terminal frame fails closed |
| restart during stream | old stream raises `StreamRestarted` |
| client cancellation | cancellation triggers bounded scoped lifecycle action |
| slow consumer | slow consumer does not produce silent unbounded behavior |

Evidence: `data/evidence_p1_systemd_k5_red_tests.txt`.

## P2 - Private Stream Terminal Protocol

Changed the private worker-to-Core `/stream` protocol from implicit
`{"data": ...}` only to explicit internal frames:

```json
{"type": "chunk", "data": "<adapter chunk>"}
{"type": "completed"}
{"type": "error", "error": {"code": "500", "message": "internal error"}}
```

Systemd Core-side parsing now accepts only `chunk`, `completed`, and `error`.
Malformed frames fail closed. EOF before `completed` or `error` fails closed.
Adapter exception text is not forwarded.

Targeted verification:

```text
python3 -m unittest tests.test_streaming_systemd_parity.SystemdStreamingParityTests.test_systemd_adapter_error_frame_is_not_returned_as_chunk_data tests.test_streaming_systemd_parity.SystemdStreamingParityTests.test_systemd_abnormal_eof_before_terminal_frame_fails_closed -v
Ran 2 tests in 1.015s
OK

python3 -m unittest discover -s tests -p 'test_streaming_systemd_auth.py' -v
Ran 2 tests in 1.012s
OK
```

Evidence: `data/evidence_p2_systemd_terminal_protocol.txt`.

## P3 - Systemd Streaming Deadlines

`SystemdTransientWorkerHost.stream()` now enforces three independent K5 bounds
from `resource_limits`:

| Bound | Enforcement |
|-------|-------------|
| `stream_first_chunk_timeout` | max wait for first internal stream frame |
| `stream_idle_timeout` | max gap between chunks |
| `stream_total_timeout` | monotonic absolute total stream deadline |

Timeout handling closes the connection, audits `INFERENCE_TIMEOUT`, recycles
only the affected worker, and raises `StreamTimeout(reason, ...)`.

Targeted verification:

```text
python3 -m unittest tests.test_streaming_systemd_parity.SystemdStreamingParityTests.test_systemd_first_chunk_timeout_uses_stream_bound tests.test_streaming_systemd_parity.SystemdStreamingParityTests.test_systemd_idle_timeout_uses_stream_bound tests.test_streaming_systemd_parity.SystemdStreamingParityTests.test_systemd_total_timeout_uses_monotonic_absolute_deadline -v
Ran 3 tests in 0.372s
OK
```

Evidence: `data/evidence_p3_systemd_stream_timeouts.txt`.

## P4 - Systemd Lifecycle And Cancellation

`SystemdTransientWorkerHost.stream()` now snapshots the active stream identity:

```text
_unit
_launch_count
_secret
```

If the worker is stopped, restarted, relaunched, or credential-rotated while a
stream is active, the old stream is invalidated and raises `StreamRestarted`
instead of completing normally.

Client/generator cancellation closes the connection, audits `STREAM_CANCELLED`,
and triggers scoped lifecycle action through the existing recycle path.

Targeted verification:

```text
python3 -m unittest tests.test_streaming_systemd_parity.SystemdStreamingParityTests.test_systemd_restart_during_active_stream_raises_stream_restarted tests.test_streaming_systemd_parity.SystemdStreamingParityTests.test_systemd_client_cancellation_does_not_leave_worker_busy -v
Ran 2 tests in 1.022s
OK
```

Evidence: `data/evidence_p4_systemd_lifecycle_cancellation.txt`.

## P5 - Protocol Correctness And Backpressure

Public protocol corrections:

| Case | Result |
|------|--------|
| streaming batch request with `{"inputs": [...]}` | HTTP 400 JSON, rejected before worker |
| app without `stream()` | NDJSON `worker_error`, code 400, message `streaming not supported` |
| systemd slow consumer | bounded by `stream_backpressure_timeout`, raises `StreamBackpressure` |

Targeted verification:

```text
python3 -m unittest tests.test_streaming_contract.StreamingContractTests.test_streaming_batch_request_is_rejected_before_worker tests.test_streaming_contract.StreamingContractTests.test_streaming_unsupported_app_returns_controlled_error -v
Ran 2 tests in 10.106s
OK

python3 -m unittest discover -s tests -p 'test_streaming_systemd_parity.py' -v
Ran 8 tests in 2.909s
OK

python3 -m unittest discover -s tests -p 'test_streaming*.py' -v
Ran 35 tests in 144.783s
OK
```

Evidence: `data/evidence_p5_stream_protocol_correctness.txt`.

## P6 - Targeted K5 Parity Verification

Targeted verification was run from `/tmp/sydeco_k5_p6_verify`.

```text
py_compile targeted: PASS
systemd parity tests: 8/8 PASS
systemd auth tests: 2/2 PASS
public streaming contract tests: 8/8 PASS
full targeted K5 streaming suite: 35/35 PASS
```

Coverage split:

| Group | Count |
|-------|-------|
| previous K5 streaming tests from 03-09 | 25 |
| new `SystemdTransientWorkerHost` parity tests | 8 |
| new public protocol correctness tests | 2 |
| total targeted K5 streaming tests | 35 |

Evidence scan found no concrete token material.

Evidence: `data/evidence_p6_k5_targeted_parity.txt`.

## P7 - Full Regression And Fresh Extraction

Full regression from isolated repo copy:

```text
python3 -m unittest discover -s tests -v
Ran 134 tests in 443.808s
OK
```

Fresh extraction regression:

```text
python3 -m unittest discover -s tests -v
Ran 134 tests in 443.150s
OK
```

Repo-wide compile from fresh extraction:

```text
find . -name '*.py' -print0 | xargs -0 python3 -m py_compile
<no output; command exited 0>
```

Hygiene:

```text
find /home/sydeco/Dev/SYDECO_LIGHTML_V2_DEV -name '__pycache__' -o -name '*.pyc' -o -name '*.pyo' | head -100
<no output>
```

Scope checks:

| Check | Result |
|-------|--------|
| full regression | 134/134 PASS |
| fresh extraction regression | 134/134 PASS |
| test count higher than 124 baseline | PASS |
| repo-wide `py_compile` | PASS |
| `__pycache__` / `.pyc` / `.pyo` in main repo | none |
| production signing key artifact | none |
| LightML 1.0.1 | untouched |
| CRA | untouched |

Evidence: `data/evidence_p7_regression_fresh_extraction.txt`.

## P8 - Report And Package Discipline

P8 actions:

| Artifact | Status |
|----------|--------|
| final English README | this file |
| daily report DOCX | generated from this README via pandoc |
| final ZIP | generated after README/report commit |
| `.zip.sha256` sidecar | generated from the exact final ZIP |
| final package evidence | recorded in `data/evidence_p8_package_report_hash.txt` |

The sidecar name must be exactly:

```text
SYDECO_LIGHTML_V2_DEV_2026-09-04.zip.sha256
```

## Evidence Table

| File | Content |
|------|---------|
| `data/evidence_p0_systemd_k5_baseline.txt` | P0 baseline, reviewer findings, coverage matrix |
| `data/evidence_p1_systemd_k5_red_tests.txt` | P1 RED systemd parity tests |
| `data/evidence_p2_systemd_terminal_protocol.txt` | P2 private terminal protocol fix |
| `data/evidence_p3_systemd_stream_timeouts.txt` | P3 systemd first/idle/total timeout proof |
| `data/evidence_p4_systemd_lifecycle_cancellation.txt` | P4 restart and cancellation parity proof |
| `data/evidence_p5_stream_protocol_correctness.txt` | P5 batch/unsupported/backpressure proof |
| `data/evidence_p6_k5_targeted_parity.txt` | P6 targeted K5 parity/security verification |
| `data/evidence_p7_regression_fresh_extraction.txt` | P7 full regression and fresh extraction |
| `data/evidence_p8_package_report_hash.txt` | P8 final report/package/hash proof |

## What Remains

K5 is submitted for review after this production-path revision. J4B Dependency
Artifact Authenticity should start only after reviewer acceptance of this K5
revision.

## Constraints Honored

- K5 revision only; no J4B implementation.
- No CRA integration.
- No F4/F6 schema migration/update handling.
- No production signing key created or used.
- No permanent installer/unit acceptance work.
- No LightML 1.0.1 modification.
- No new streaming protocol such as WebSocket or SSE.
- No external/cloud dependency introduced; wording remains "needs no Internet
  beyond OS dependencies".
- Evidence files are secret-free.
- Final package uses `.zip` plus correctly named `.zip.sha256` sidecar.

## Conclusion

The reviewer-blocking K5 production-path parity gaps have been addressed.
`SystemdTransientWorkerHost` now has explicit private stream terminal frames,
independent first/idle/total stream deadlines, restart invalidation, bounded
cancellation behavior, and slow-consumer backpressure handling. Public protocol
gaps for streaming batch and unsupported streaming are closed. Targeted K5
tests pass 35/35, and full regression plus fresh extraction pass 134/134.

Status: **SYDECO LIGHTML UNIVERSAL RUNTIME V2 - DEVELOPMENT / PROOF OF CONCEPT
- K5 REVISION SUBMITTED / AWAITING REVIEW.**
