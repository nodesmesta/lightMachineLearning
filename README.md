# 07092026

## Summary

This report covers **PHASE 2 / DAY 4C - FINAL K5 LIFECYCLE CLOSURE** for
SYDECO LightML V2. It is a direct continuation of the 04-09 K5 systemd
production-path revision review. That review recognized that Day 4B had already
closed almost all of the previous K5 parity gaps: explicit internal terminal
framing, first/idle/total stream deadlines, generation tracking, cancellation,
batch-stream rejection, unsupported-stream handling, and systemd backpressure.
The reviewer also independently confirmed that the K5 streaming tests and full
regression passed.

Reviewer verdict for the 04-09 submission:

> Very strong revision, but I do not yet accept K5 as completely closed.

The remaining blocker was not a broad design failure. It was one production
lifecycle race in `SystemdTransientWorkerHost.stream()`:

**When an active systemd stream is already blocked inside `resp.readline()` and
the worker is restarted concurrently, the old stream can return `StreamTimeout`
or another transport/protocol error instead of `StreamRestarted`.**

The Day 4C work therefore does three things:
- reproduces that lifecycle race with deterministic RED tests before changing
  production code;
- applies the smallest production-path fix so stale worker generation takes
  precedence over timeout, EOF, successful old-generation reads, and transport
  failures;
- proves the result through systemd parity tests, complete K5 streaming tests,
  full regression, isolated-copy regression, compile checks, and final package
  integrity checks.

P6 is not reported as a separate technical evidence section because P6 is the
documentation/reporting process itself. The documentation output is this
workspace README and the generated daily report DOCX. The technical evidence is
recorded in P0-P5, while P7 records final package/report/hash discipline.

Status:

**SYDECO LIGHTML UNIVERSAL RUNTIME V2 - DEVELOPMENT / PROOF OF CONCEPT - K5 DAY
4C SUBMITTED / AWAITING REVIEW.**

## Scope

This Day 4C work is limited to final K5 lifecycle closure:
- add deterministic RED tests for concurrent restart while systemd stream I/O
  is blocked;
- give stale worker generation precedence over timeout, EOF, and transport
  failure interpretations;
- verify systemd parity, the full K5 streaming surface, full regression, and
  isolated-copy regression;
- prepare workspace evidence and this report.

This work does not start J4B Dependency Artifact Authenticity, CRA integration,
F4/F6 schema migration/update handling, production signing, installer
acceptance, WorkerManager redesign, a new streaming protocol, or LightML 1.0.1
changes.

## Commit Log

| Commit | Purpose |
|---|---|
| `2bb6e29` | Add Day 4C lifecycle RED tests |
| `4af8808` | Fix systemd stream restart precedence |
| `56aee2e` | Record systemd parity verification |
| `04c3938` | Record full streaming verification |
| `753a732` | Record regression verification |
| `c39ddef` | Record package procedure |
| post-review cleanup | Sync source README and close the stream cleanup warning |

Repository state after the P7 package-procedure commit and before post-review
cleanup:

```text
HEAD: c39ddef
commit count: 27
git status: clean
```

## P0 - Day 4C Baseline

P0 was read-only. It confirmed the current baseline and the exact reviewer
finding before any Day 4C code changes.

Baseline state:

```text
git -C /home/sydeco/Dev/SYDECO_LIGHTML_V2_DEV rev-parse --short HEAD
9c0d95d

git -C /home/sydeco/Dev/SYDECO_LIGHTML_V2_DEV rev-list --count HEAD
21

git -C /home/sydeco/Dev/SYDECO_LIGHTML_V2_DEV status --short
<no output; working tree clean>
```

Baseline test definitions:

```text
streaming tests: 35
total tests: 134
```

Confirmed baseline findings:
- `SystemdTransientWorkerHost.stream()` already captured stream identity using
  `_unit`, `_launch_count`, and `_secret`;
- `_stream_stale()` already existed, but stale-generation precedence was not
  checked in all reviewer-required locations;
- `resp.readline()` returned data without an immediate stale check before EOF
  or frame interpretation;
- `socket.timeout` could become `StreamTimeout` before stale generation won;
- EOF before terminal frame could become protocol failure before stale
  generation won;
- transport failure could become `WorkerNotReady` before stale generation won;
- the slow-consumer systemd test used generic `Exception`, not exact
  `StreamBackpressure`.

Evidence: `data/evidence_p0_day4c_lifecycle_baseline.txt`.

## P1 - RED Lifecycle Tests

P1 added deterministic RED coverage before production code changes. The tests
exercise `SystemdTransientWorkerHost`, with the systemd manager faked but the
Core-side production host, loopback HTTP stream, and credential-forwarding path
using the real code under test.

New tests:
- restart while blocked in the read path;
- restart followed by EOF;
- restart followed by active-stream transport failure.

Tightened existing test:
- slow-consumer systemd behavior now expects `StreamBackpressure` exactly.

RED evidence:

```text
python3 -m unittest discover -s tests -p 'test_streaming_systemd_parity.py' -v

test_systemd_concurrent_restart_during_blocked_read_raises_stream_restarted ... FAIL
test_systemd_restart_followed_by_eof_raises_stream_restarted ... FAIL
test_systemd_restart_followed_by_transport_failure_raises_stream_restarted ... FAIL
test_systemd_slow_consumer_has_bounded_behavior ... ok

Ran 11 tests in 4.134s
FAILED (failures=3)
```

Failure meanings:
- blocked read after concurrent restart reported `StreamTimeout`, not
  `StreamRestarted`;
- EOF after restart reported `RuntimeError`, not `StreamRestarted`;
- transport failure after restart reported `RuntimeError`, not
  `StreamRestarted`;
- tightened backpressure behavior already raised `StreamBackpressure`.

Evidence: `data/evidence_p1_day4c_concurrent_restart_red_tests.txt`.

## P2 - Lifecycle Precedence Fix

P2 implemented the minimal production-path fix in
`SystemdTransientWorkerHost.stream()`.

What changed:
- added a local `_raise_if_stream_stale()` helper;
- reused the existing stream identity snapshot: `_unit`, `_launch_count`,
  `_secret`;
- checked stale generation before loop iteration processing;
- checked stale generation immediately after `line = resp.readline()` returns;
- checked stale generation before accepting chunk and completed frames;
- checked stale generation before abnormal EOF becomes protocol failure;
- checked stale generation before `socket.timeout` becomes `StreamTimeout`;
- checked stale generation before transport failure becomes `WorkerNotReady`.

Behavior intentionally preserved when the stream is not stale:
- first-chunk, idle, and total timeout still raise `StreamTimeout`;
- abnormal EOF still fails closed;
- transport failure still maps to `WorkerNotReady`;
- adapter error and malformed protocol behavior remain unchanged;
- public server mapping remains unchanged.

Targeted GREEN evidence:

```text
python3 -m unittest discover -s tests -p 'test_streaming_systemd_parity.py' -v

Ran 11 tests in 4.431s
OK
```

Evidence: `data/evidence_p2_day4c_lifecycle_precedence_green.txt`.

## P3 - Systemd Parity Verification

P3 re-ran the targeted systemd parity surface after P2.

Systemd parity count:

```text
11
```

Backpressure assertion check:

```text
499:        with self.assertRaises(StreamBackpressure):
```

Targeted result:

```text
python3 -m unittest discover -s tests -p 'test_streaming_systemd_parity.py' -v

Ran 11 tests in 4.437s
OK
```

Coverage confirmed:
- first chunk timeout;
- idle timeout;
- total timeout;
- adapter error frame handling;
- abnormal EOF fail-closed behavior;
- restart during active stream;
- deterministic concurrent restart during blocked read;
- restart followed by EOF;
- restart followed by active-stream transport failure;
- client cancellation;
- slow-consumer backpressure with exact `StreamBackpressure` assertion.

One non-fatal `ResourceWarning` was observed during one P3 run from the
stdlib/socket cleanup path. A supporting analysis note was written in
`data/report.md`. The warning did not fail the suite and did not alter the P3
result.

Evidence: `data/evidence_p3_day4c_systemd_parity_targeted.txt`.

## P4 - Complete K5 Streaming Verification

P4 ran the complete K5 streaming surface.

Streaming test count:

```text
38
```

Streaming split:

| Test Module | Count |
|---|---:|
| `test_streaming_isolation.py` | 5 |
| `test_streaming_timeout.py` | 3 |
| `test_streaming_contract.py` | 8 |
| `test_streaming_systemd_parity.py` | 11 |
| `test_streaming_systemd_auth.py` | 2 |
| `test_streaming_cancellation.py` | 2 |
| `test_streaming_security.py` | 5 |
| `test_streaming_backpressure.py` | 2 |

Complete K5 streaming result:

```text
python3 -m unittest discover -s tests -p 'test_streaming*.py' -v

Ran 38 tests in 148.226s
OK
```

P4 confirmed:
- no previous streaming test disappeared;
- 3 Day 4C lifecycle tests were added;
- systemd authentication and credential rotation passed;
- systemd lifecycle race tests passed;
- timeout, cancellation, backpressure, lifecycle isolation, protocol framing,
  malformed input, oversize input, unsupported streaming, and batch rejection
  tests passed.

Two logged tracebacks appeared in expected negative/security test paths. Both
tests completed with `ok`, and the final suite result was `OK`.

Evidence: `data/evidence_p4_day4c_streaming_full_targeted.txt`.

## P5 - Full Regression And Isolated Copy

P5 ran full regression, isolated-copy regression, repo-wide compile, and final
hygiene checks.

Current committed state before P5 evidence commit:

```text
HEAD: 04c3938
commit count: 25
git status: clean
```

Test counts:

```text
total tests: 137
streaming tests: 38
```

Full regression in the main repository:

```text
python3 -m unittest discover -s tests -v

Ran 137 tests in 448.448s
OK
```

Isolated-copy regression:

```text
copy path: /tmp/sydeco_day4c_p5_verify

python3 -m unittest discover -s tests -v

Ran 137 tests in 451.480s
OK
```

`py_compile` verification:

```text
find /home/sydeco/Dev/SYDECO_LIGHTML_V2_DEV -name '*.py' -print0 | xargs -0 python3 -m py_compile
<no output; command exited 0>

find /tmp/sydeco_day4c_p5_verify -name '*.py' -print0 | xargs -0 python3 -m py_compile
<no output; command exited 0>
```

Generated cache cleanup:

```text
main repo generated __pycache__/.pyc/.pyo entries before cleanup: 52
isolated copy generated __pycache__/.pyc/.pyo entries before cleanup: 52

main repo generated __pycache__/.pyc/.pyo entries after cleanup: 0
isolated copy generated __pycache__/.pyc/.pyo entries after cleanup: 0
```

Scope checks:

```text
production signing key / prod key / CRA search in dev repo:
<no output>
```

P5 confirmed:
- full regression passed `137/137`;
- isolated-copy regression passed `137/137`;
- repo-wide `py_compile` passed;
- isolated-copy `py_compile` passed;
- generated Python cache artifacts were removed after explicit approval;
- no production signing key or CRA artifact was found in the dev repo;
- Day 2 worker authentication remains intact;
- Day 3B J1/J2 dependency isolation remains intact;
- timeout recovery remains intact;
- application isolation remains intact;
- credential lifecycle remains intact.

Evidence: `data/evidence_p5_day4c_regression_fresh_extraction.txt`.

## P7 - Package, Report, And Hash

P7 corrected the package/report/hash discipline called out by the reviewer.

Canonical package evidence:

```text
data/evidence_p7_day4c_package_report_hash.txt
```

That file is included inside the source ZIP and intentionally does not contain
the final ZIP hash, because embedding the final ZIP hash inside the ZIP would
create a circular hashing problem.

Source hygiene state used for packaging:

```text
git status: clean
generated __pycache__/.pyc/.pyo entries: 0
```

Report artifact:

```text
/home/sydeco/Dev/Report/week-6/JAMALUDIN_DailyReport_07-09-2026.docx
```

Package artifacts:

```text
/home/sydeco/Dev/Task/week-6/07-09-2026/data/SYDECO_LIGHTML_V2_DEV_2026-09-07.zip
/home/sydeco/Dev/Task/week-6/07-09-2026/data/SYDECO_LIGHTML_V2_DEV_2026-09-07.zip.sha256
```

ZIP integrity:

```text
unzip -t /home/sydeco/Dev/Task/week-6/07-09-2026/data/SYDECO_LIGHTML_V2_DEV_2026-09-07.zip

No errors detected in compressed data of /home/sydeco/Dev/Task/week-6/07-09-2026/data/SYDECO_LIGHTML_V2_DEV_2026-09-07.zip.
```

SHA-256 sidecar verification:

```text
sha256sum -c SYDECO_LIGHTML_V2_DEV_2026-09-07.zip.sha256

SYDECO_LIGHTML_V2_DEV_2026-09-07.zip: OK
```

Package content check:

```text
SYDECO_LIGHTML_V2_DEV/data/evidence_p7_day4c_package_report_hash.txt
SYDECO_LIGHTML_V2_DEV/sydeco_lightml_core/worker.py
SYDECO_LIGHTML_V2_DEV/tests/test_streaming_systemd_parity.py
```

No `.zip.sha256`, `__pycache__`, `.pyc`, or `.pyo` entry appeared in the package
content check.

Evidence: `data/evidence_p7_day4c_package_report_hash.txt`.

## P8 - Final Handoff Summary

Final repository state:

```text
Recorded in the external workspace README after the final ZIP and sidecar are
generated.
```

Final verification summary:

| Check | Result |
|---|---:|
| systemd parity tests | 11/11 PASS |
| complete K5 streaming tests | 38/38 PASS |
| full regression in main repo | 137/137 PASS |
| isolated-copy regression | 137/137 PASS |
| repo-wide `py_compile` | PASS |
| isolated-copy `py_compile` | PASS |
| generated cache artifacts | 0 |
| final ZIP integrity | PASS |
| final `.zip.sha256` verification | PASS |

Final artifacts:

| Artifact | Status |
|---|---|
| daily report DOCX | generated |
| source ZIP | generated |
| external SHA-256 sidecar | verified |
| canonical package evidence | included in ZIP |

Final artifact paths:
- daily report DOCX:
  `/home/sydeco/Dev/Report/week-6/JAMALUDIN_DailyReport_07-09-2026.docx`
- source ZIP:
  `data/SYDECO_LIGHTML_V2_DEV_2026-09-07.zip`
- external SHA-256 sidecar:
  `data/SYDECO_LIGHTML_V2_DEV_2026-09-07.zip.sha256`
- canonical package evidence:
  `data/evidence_p7_day4c_package_report_hash.txt`

Final SHA-256:

```text
Recorded in the external .zip.sha256 sidecar generated after the final ZIP.
```

Explicit non-scope confirmation:
- J4B Dependency Artifact Authenticity was not started;
- CRA integration was not started;
- F4/F6 schema migration/update handling was not started;
- no production signing key was created or used;
- permanent installer/unit acceptance was not started;
- LightML 1.0.1 was not modified;
- WorkerManager was not redesigned;
- no new streaming protocol was introduced.

## Evidence Table

| Phase | Evidence | Content |
|---|---|---|
| P0 | `evidence_p0_day4c_lifecycle_baseline.txt` | Baseline and reviewer finding |
| P1 | `evidence_p1_day4c_concurrent_restart_red_tests.txt` | RED lifecycle tests |
| P2 | `evidence_p2_day4c_lifecycle_precedence_green.txt` | Lifecycle precedence GREEN |
| P3 | `evidence_p3_day4c_systemd_parity_targeted.txt` | Systemd parity verification |
| P4 | `evidence_p4_day4c_streaming_full_targeted.txt` | Complete K5 streaming verification |
| P5 | `evidence_p5_day4c_regression_fresh_extraction.txt` | Full and isolated-copy regression |
| P7 | `evidence_p7_day4c_package_report_hash.txt` | Package/report/hash procedure |
| Note | `report.md` | Non-fatal ResourceWarning observation |

## What Remains

K5 Day 4C is packaged and ready for reviewer submission. J4B Dependency
Artifact Authenticity must start only after reviewer acceptance of this K5 Day
4C package.

## Constraints Honored

- K5 Day 4C lifecycle closure only.
- No J4B implementation.
- No CRA integration.
- No F4/F6 schema migration/update handling.
- No production signing key created or used.
- No permanent installer/unit acceptance work.
- No LightML 1.0.1 modification.
- No WorkerManager redesign.
- No new streaming protocol such as WebSocket or SSE.
- No external/cloud dependency introduced; the deliverable needs no Internet
  beyond OS dependencies.
- Evidence files are secret-free.
- Final package sidecar is external and uses `.zip.sha256`.

## Conclusion

Day 4C closes the remaining K5 lifecycle race identified in the 04-09 review.
The systemd production stream now gives stale worker generation precedence over
timeout, EOF, and transport-failure interpretations, including after a blocking
read succeeds but before any old-generation frame is accepted.

The required RED tests were added first and reproduced the reviewer issue.
After the minimal fix, systemd parity passed `11/11`, complete K5 streaming
passed `38/38`, full regression passed `137/137`, isolated-copy regression
passed `137/137`, and repo-wide compile checks passed. The final source ZIP and
external `.zip.sha256` sidecar are generated after source finalization, with the
exact archive hash recorded externally to avoid circular package metadata.

Status: **SYDECO LIGHTML UNIVERSAL RUNTIME V2 - DEVELOPMENT / PROOF OF CONCEPT
- K5 DAY 4C SUBMITTED / AWAITING REVIEW.**
