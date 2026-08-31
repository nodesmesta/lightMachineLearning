# 28082026

## Summary

This session executes the reviewer's **PHASE 2 / DAY 2 — AUTHENTICATED CORE
<-> WORKER CHANNEL** directive from `Jamaludin REPORT 27 AUGUST REVIEWED.docx`
(review of the 27-08-2026 submission, copied into this day's folder
`week-4/28-08-2026/`). Review verdict (verbatim): **"PHASE 2 / DAY 1C —
ACCEPTED."** (97/100) — process isolation CLOSED, timeout containment CLOSED,
dev-host timeout semantics CLOSED — and the reviewer authorized Day 2:
"Today, Core and worker communicate over 127.0.0.1. That protects them from
external network access, but loopback does not authenticate the caller.
Another local process could potentially contact the worker." The exact order
P0..P6 was followed, mapped to this day's P1..P8.

The design was already LOCKED on 27-08 (decision D5, all five
recommendations): systemd `LoadCredential=` backed by a root-only mode-600
file; Bearer per-request + `hmac.compare_digest`; /health/ready
authenticated; credential in-memory per generation + ephemeral root-only
file removed on stop; InProcess dev equivalent in-memory rotated per recycle.

Day 2 implementation and verification are complete; final packaging follows after the corrected source and evidence are committed:

1. **P0 (reviewer) / P1** — the `_recycling` check/set in
   `SystemdTransientWorkerHost._recycle_after_timeout` was NOT lock-protected
   while the Core HTTP server is multithreaded (reviewer finding confirmed in
   the source). Protected by a small local lock; a deterministic barrier test
   proves two simultaneous timeouts produce exactly ONE recycle, one
   replacement generation, one WORKER_RESTART.
2. **P2** — cryptographically random secret per worker generation
   (`secrets.token_hex(32)`), unique per app, rotated on every
   recycle/restart, never reused across applications.
3. **P3** — secure delivery: `LoadCredential=worker-secret:<path>` via the
   transient-unit property mechanism (systemd 249 has no `--load-credential`
   CLI option), ephemeral root-only (0600) file unique per launch, removed on
   stop; the worker reads `$CREDENTIALS_DIRECTORY/worker-secret` (production)
   or `--credential-file` (dev/test; the PATH may appear in argv, the SECRET
   never does) and FAILS CLOSED without one.
4. **P4** — the worker requires the Bearer credential on ALL internal
   endpoints (`POST /infer`, `GET /health/ready`, `GET /health/live`) — no
   unauthenticated worker endpoint; constant-time `hmac.compare_digest`;
   auth failures are audited worker-side (`AUTH_FAILURE` in the worker's own
   data-dir audit JSONL — C2 elaboration).
5. **P5** — credential isolation proven: App A token -> App A accepted;
   App A token -> App B rejected; old generation token -> new generation
   rejected; new token -> accepted; one app's auth failure does not affect
   another.
6. **P6** — leakage sweep: 0 secret occurrences in command line,
   `/proc/<pid>/cmdline`, `/proc/<pid>/environ` (REAL systemd worker),
   manifest, registry, Core + worker audit, normal logs, client error
   responses, package, git repository. Only allowed locations: tightly
   controlled runtime memory + the systemd credential mechanism.
7. **P7** — the reviewer's 13-row acceptance matrix PASSES both
   non-privileged (real `worker_runtime` process + credential file, honest
   dev labels, D5 #5) AND on the REAL systemd path (root harness 31/31
   including LoadCredential=, $CREDENTIALS_DIRECTORY in the worker env,
   /proc leakage, timeout 504 -> recycle -> rotated secret -> old 401 /
   new 200, exactly one WORKER_RESTART).
8. **No regression** — full suite **83/83 PASS** (63 pre-existing + 20 new)
   in the repo AND from a fresh extraction; py_compile 17/17; 0 dev-path
   occurrences; 0 new `__pycache__`.

The corrected source and regression test are included in the final repository state; the final commit and clean-tree status are verified during packaging.

Final status remains verbatim:
**SYDECO LIGHTML UNIVERSAL RUNTIME V2 — DEVELOPMENT / PROOF OF CONCEPT —
AWAITING REVIEW.**

---

## P1 — Production recycle concurrency (reviewer P0)

**File changed:** `sydeco_lightml_core/worker.py` — `SystemdTransientWorkerHost`
gains a small local `_recycle_lock` around the `_recycling` check/set and the
reset in `finally` of `_recycle_worker`.

**What:** the reviewer's finding is confirmed in the source: `_recycle_after_timeout`
did `if self._recycling: return; self._recycling = True` without a lock, while
the Core HTTP server is a `ThreadingHTTPServer` — two simultaneous timeouts on
the same capability could both pass the check and spawn two recycle threads.
The fix follows the reviewer's instruction exactly ("protect the
production-host recycle transition with a small local lock, without
redesigning WorkerManager"):

| Step | Mechanism |
|---|---|
| two simultaneous requests hit the timeout condition | both `infer()` calls raise `socket.timeout` on the same capability |
| `_recycle_after_timeout` | check/set of `_recycling` now atomic under `_recycle_lock`; the second caller sees `_recycling=True` and returns without spawning |
| exactly one recycle thread | one `systemd-run` relaunch (unit `-r1`) |
| exactly one replacement generation | one fresh secret + one fresh credential file |
| exactly one timeout WORKER_RESTART | audited once by the single recycle thread; readiness BACKOFF -> READY |

The deterministic test drives two threads through a barrier (not sleeps) so
both timeouts land concurrently:

| Assertion | Result (live) |
|---|---|
| both requests time out (InferenceTimeout) | PASS (`["timeout", "timeout"]`) |
| exactly one recycle (2 systemd-run total: initial + 1 relaunch) | PASS |
| readiness recovers to READY | PASS |
| exactly one `WORKER_RESTART` detail="timeout recycle" | PASS |
| surviving generation serves a normal inference | PASS (200) |

**Evidence:** `data/evidence_p1_recycle_concurrency.txt`.

## P2 — Per-generation worker secret (reviewer P1)

**File changed:** `sydeco_lightml_core/worker.py` — both hosts generate and
hold a per-generation secret; `InProcessWorkerHost` rotates it in `start()`,
`restart()` and the timeout-recycle publish; `SystemdTransientWorkerHost`
generates it in `start()` (before every launch).

**What:** the reviewer's P1 directive, verbatim: "Generate a cryptographically
random secret for every worker generation. It must be: unique per
application; unique per generation; rotated after every worker
recycle/restart; never reused across applications. Use a strong random source
such as secrets.token_bytes(32)." Implemented with `secrets.token_hex(32)`
(the hex form of `token_bytes(32)`, 64 hex chars).

| Requirement (reviewer P1) | Mechanism | Proof |
|---|---|---|
| unique per application | Core holds one secret per worker host/app | App A secret != App B secret (P2 test) |
| unique per generation | fresh `token_hex(32)` in every `start()` | generation 1 != generation 2 (restart) |
| rotated after every recycle/restart | `restart()` and the timeout-recycle publish a fresh secret | root harness: secret A1 != A2 after timeout recycle |
| never reused across applications | secrets never shared; no persistence | isolation test (P5) |

**Evidence:** `data/evidence_p2_p3_secret_delivery.txt`.

## P3 — Secure credential delivery (reviewer P2)

**File changed:** `sydeco_lightml_core/worker.py` (credential file lifecycle +
`LoadCredential=` property), `sydeco_lightml_core/core.py` (context
`credential_dir`), `sydeco_lightml_core/worker_runtime.py`
(`load_worker_secret()` + `--credential-file`), `tests/test_security.py`
(test_13 passes a credential file — the worker is fail-closed now).

**What:** the reviewer's P2 directive: "For the systemd production path, use
the design he already selected: systemd LoadCredential=." The transient unit
property is set via the generic `--property=LoadCredential=worker-secret:<path>`
mechanism (systemd 249 has no `--load-credential` CLI option). The worker
reads `$CREDENTIALS_DIRECTORY/worker-secret` (production) or `--credential-file`
(dev/test approximation; the PATH may appear in argv — the SECRET never
does). No credential -> fail-closed (`WorkerRuntimeError` -> non-zero exit ->
unit fails).

| Reviewer P2 requirement | Implementation | Proof |
|---|---|---|
| systemd LoadCredential= | `LoadCredential=worker-secret:<abs path>` in the transient unit properties | property present in the systemd-run argv; root harness: `CREDENTIALS_DIRECTORY` set in the real worker env |
| NOT command line / Environment / manifest | secret only in the 0600 file; argv/Environment carry the path at most | focused test asserts the secret is absent from argv and `Environment=` values; root /proc sweep 0/0 |
| root-controlled credential file | ephemeral file created mode 0600 in `<core-data>/secrets/`, unique per launch | mode 600 assertion; per-launch file name |
| removed when the unit stops | `stop()` unlinks the credential file | focused test + root harness ("credential file removed on stop") |
| serving Core must NOT become permanently root | all privileged work (systemd-run, credential file) lives in the narrow supervisor/install boundary; documented in the code docstring | the harness (root) runs the Core; the serving Core itself never elevates |

**Evidence:** `data/evidence_p2_p3_secret_delivery.txt`,
`data/evidence_p7_root_acceptance.txt`.

## P4 — Authenticate the complete internal worker API (reviewer P3)

**File changed:** `sydeco_lightml_core/worker_runtime.py` — `_check_bearer()`
(constant-time), request-local token extraction, auth gate on ALL endpoints,
`_audit_auth_failure()` (worker-side AUTH_FAILURE JSONL).

The regression test in `tests/test_channel_auth_b.py` deterministically proves
that concurrent valid and invalid tokens cannot share request state.

**What:** the reviewer's P3 directive, verbatim: "Require the Bearer
credential on: POST /infer, GET /health/ready, GET /health/live. In other
words: No unauthenticated worker HTTP endpoint. Use constant-time comparison
(hmac.compare_digest)."

| Endpoint | no token | wrong token | correct token |
|---|---|---|---|
| POST /infer | 401 | 401 | 200 |
| GET /health/ready | 401 | 401 | 200 |
| GET /health/live | 401 | 401 | 200 |

The 401 body is a generic `{"error": {... "unauthorized"}}` — no secret, no
hint. Auth failures are recorded by the worker itself in
`<data_dir>/audit/audit.jsonl` as `AUTH_FAILURE` (the Core cannot observe 401s
inside another process; a legitimate granular elaboration of C2 — the
proposal does not lock event names, 24-08 precedent). The event carries
app_id, source endpoint and timestamp — never the credential or the presented
token (leakage-checked).

**Evidence:** `data/evidence_p4_auth_endpoints.txt`.

## P5 — Credential isolation (reviewer P4)

**File changed:** tests (`tests/test_channel_auth_b.py`).

**What:** the reviewer's P4 directive: "Prove: App A token -> App A =
accepted; App A token -> App B = rejected; old App A generation token -> new
App A generation = rejected; new App A token -> new generation = accepted."

| Isolation case | Expected | Result (live) |
|---|---|---|
| App A token against App A | accepted (200) | PASS |
| App A token against App B | rejected (401) | PASS |
| old App A generation token -> new generation | rejected (401) | PASS |
| new App A token -> new generation | accepted (200) | PASS |
| one app's auth failure does not affect another | App B's storm leaves App A serving | PASS |

**Evidence:** `data/evidence_p5_credential_isolation.txt`.

## P6 — Leakage testing (reviewer P5)

**File changed:** tests (`tests/test_channel_auth_b.py`).

**What:** the reviewer's P5 directive: search explicitly for the secret in
command line, /proc/<pid>/cmdline, /proc/<pid>/environ, manifest, registry,
audit log, normal logs, client error responses, package, Git repository —
expected result **0 secret occurrences**; the only allowed locations are
tightly controlled runtime memory and the systemd credential mechanism.

| Location | Expected | Result (live) |
|---|---|---|
| command line (spawned worker argv) | 0 | PASS |
| /proc/<pid>/cmdline (REAL systemd worker) | 0 | PASS |
| /proc/<pid>/environ (REAL systemd worker) | 0 | PASS |
| manifest / registry / source tree | 0 | PASS |
| Core audit + worker audit (incl. AUTH_FAILURE events) | 0 | PASS |
| worker stdout/stderr (normal logs) | 0 | PASS |
| client error responses (401 body) | 0 | PASS |
| package (ZIP, incl. evidence files) | 0 | PASS (meta-check before packaging) |
| git repository (committed files) | 0 | PASS |

**Evidence:** `data/evidence_p6_leakage.txt`, `data/evidence_p7_root_acceptance.txt`.

## P7 — Required acceptance matrix + no regression (reviewer P6)

**File changed:** tests (`tests/test_channel_auth_c.py`, 6 acceptance-matrix
tests) + the privileged root harness (external, `/tmp/hermes-day2-root-harness.py`).

**What:** the reviewer's 13-row acceptance table, verbatim, proven on the dev
equivalents AND on the REAL systemd path:

| Test | Expected | Non-privileged | Root (real systemd) |
|---|---|---|---|
| /infer, no token | 401 | PASS | PASS |
| /infer, wrong token | 401 | PASS | PASS |
| /infer, correct token | 200 | PASS | PASS |
| /health/ready, no token | 401 | PASS | PASS |
| /health/ready, correct token | 200 | PASS | PASS |
| /health/live, no token | 401 | PASS | PASS |
| /health/live, correct token | 200 | PASS | PASS |
| App A token against App B | 401 | PASS | PASS |
| old token after recycle | 401 | PASS | PASS |
| new token after recycle | 200 | PASS | PASS |
| token absent from logs/manifest/registry | PASS | PASS | PASS |
| one app authentication failure does not affect another | PASS | PASS | PASS |
| simultaneous timeout causes one recycle only | PASS | PASS (WORKER_RESTART count=1) | PASS |

The ROOT harness (31/31 PASS, self-cleaning: stops units incl. -rN relaunches,
removes capability users and temp dirs) additionally proves on the REAL
systemd path: `LoadCredential=` delivery with `$CREDENTIALS_DIRECTORY` set in
the worker env, ephemeral credential file mode 600 removed on stop, /proc
leakage 0/0 on the real worker PID, `AUTH_FAILURE` audited worker-side
(count=4 from the wrong-token probes), timeout STALL -> InferenceTimeout (the
504 the Core edge returns) -> recycle -> secret rotated -> old 401 / new 200
-> exactly one timeout WORKER_RESTART.

No regression — reviewer: "rerun all existing 63 tests plus the new tests,
both: repository -> PASS and fresh extraction -> PASS":

| Check | Result (live) |
|---|---|
| Focused Fase A (`test_channel_auth.py`) | 7/7 PASS |
| Focused Fase B (`test_channel_auth_b.py`) | 7/7 PASS |
| Focused acceptance matrix (`test_channel_auth_c.py`) | 6/6 PASS |
| `test_security.py` (incl. updated test_13) | 13/13 PASS |
| `test_timeout_recovery.py` | 11/11 PASS |
| FULL suite IN THE REPO | **83/83 PASS** (Ran 83 tests ... OK) |
| FULL suite FROM FRESH EXTRACTION | **83/83 PASS**, 0 dev-path, py_compile 17/17 |
| 0 new `__pycache__` / `.pyc` today | PASS |

**Evidence:** `data/evidence_p7_acceptance_matrix.txt`,
`data/evidence_p7_root_acceptance.txt` (31/31),
`data/evidence_p8_fresh_extraction.txt`, `data/evidence_regression_full_suite.txt`.

## P8 — Evidence + daily report + final package

- 9 evidence files in `data/` (list in the Evidence table below), all
  secret-free (meta-check: the day's `data/` + README are swept for the test
  secret values before packaging).
- This README.md (English, single source of truth) and the generated
  `JAMALUDIN_DailyReport_28-08-2026.docx` (pandoc, reference-doc = the
  27-08-2026 report, GitHub table fixes applied, docx validated).
- Corrected source and regression test verified; final commit and clean-tree
  status are recorded after the final packaging commit.
- Package: `SYDECO_LIGHTML_V2_DEV_2026-08-28.zip` + `.sha256` sidecar
  containing source + .git + tests + README + ALL `data/` evidence files
  (reports and archives correspond exactly); listing + entry count verified.
- Final statement (verbatim): **LightML 1.0.1 untouched. CRA untouched. No
  production signing key created.**

---

## Bugs / architecture issues found and fixed (2026-08-28)

| # | Issue (root cause) | Fix |
|---|---|---|
| 1 | (reviewer P0 finding, confirmed) `SystemdTransientWorkerHost._recycle_after_timeout` check/set of `_recycling` was NOT lock-protected while the Core HTTP server is multithreaded — two simultaneous timeouts could spawn two recycle threads | small local `threading.Lock` around check/set + reset in `finally`; deterministic barrier test (P1) |
| 2 | worker HTTP endpoints (`/infer`, `/health/ready`, `/health/live`) had NO authentication — loopback ≠ authenticated channel (D2 note) | Bearer required on ALL endpoints, `hmac.compare_digest`, 401 generic, AUTH_FAILURE worker-side audit (P4) |
| 3 | no per-generation secret existed; a fresh generation could keep using an old credential | `secrets.token_hex(32)` per generation, rotated on start/restart/recycle (P2/P3) |
| 4 | worker had no secure delivery path (nothing to read) | `load_worker_secret()`: `$CREDENTIALS_DIRECTORY/worker-secret` (systemd) or `--credential-file` (dev); fail-closed (P3) |
| 5 | `test_13_worker_binds_loopback_only` probed `/health/ready` WITHOUT a token — would fail once auth landed | probe sends the credential (P4 ripple) |
| 6 | (root-harness debugging, dev-only) worker unit `PrivateTmp=yes` remaps `/tmp` — paths under /tmp invisible to the worker ("not ready within 30s") | harness workdir moved to `/var/lib` (24-08 pitfall, reapplied) |
| 7 | (root-harness debugging, dev-only) leftover transient units (`-r1`) from a crashed run block the next launch ("Unit already exists"); audit read raced the recycle thread | harness cleanup stops all `-rN` units; audit read waits 0.5 s |

## Verification (before declaring done)

| Check | Result |
|---|---|
| Focused Fase A / B / acceptance matrix | 7/7 / 7/7 / 6/6 PASS |
| FULL suite IN THE REPO | 83/83 PASS |
| Full suite FROM FRESH EXTRACTION | 83/83 PASS; 0 dev paths; py_compile 17/17 |
| ROOT harness (real systemd, LoadCredential, /proc sweep) | 31/31 PASS |
| Leakage (all reviewer P5 locations, incl. real /proc) | 0 occurrences |
| 0 new `__pycache__` / `.pyc` | PASS |
| git commit + clean tree | verified during final packaging |
| Evidence files secret-free | PASS (meta-sweep) |
| LightML 1.0.1 / CRA / production key | untouched / untouched / not created |

## Evidence table

| File | Content |
|---|---|
| `data/evidence_p1_recycle_concurrency.txt` | **NEW**: P1 (reviewer P0) — deterministic concurrent-timeout test, one recycle/generation/WORKER_RESTART |
| `data/evidence_p2_p3_secret_delivery.txt` | **NEW**: P2/P3 — per-generation secret, LoadCredential property, 0600 file lifecycle, fail-closed worker |
| `data/evidence_p4_auth_endpoints.txt` | **NEW**: P4 — Bearer on all endpoints, 401 matrix, AUTH_FAILURE audit |
| `data/evidence_p5_credential_isolation.txt` | **NEW**: P5 — App A vs App B, old vs new generation |
| `data/evidence_p6_leakage.txt` | **NEW**: P6 — 0 occurrences in repo/proc/logs/errors |
| `data/evidence_p7_acceptance_matrix.txt` | **NEW**: P7 non-privileged — 13-row acceptance table |
| `data/evidence_p7_root_acceptance.txt` | **NEW**: P7 ROOT — real-systemd harness **31/31 PASS** |
| `data/evidence_p8_fresh_extraction.txt` | **NEW**: fresh tree — 0 dev paths, 17/17 compile, 83/83 |
| `data/evidence_regression_full_suite.txt` | **NEW**: full suite in repo — 83/83 |
| `data/SYDECO_LIGHTML_V2_DEV_2026-08-28.zip` + `.sha256` | **NEW**: Day-2 package — source + .git + tests + README + all evidence |

## What remains

- The reviewer's verification of this Day-2 report + package.
- Phase 2 hardening continues (reviewer's agreed order): wheelhouse/venv per
  app (J1/J2), streaming (K5), update handling + schema migration (F4/F6),
  production signing-key infrastructure (R6 infra).
- Permanent systemd unit files (from the same property set) when the full
  installer (3.1) lands.
- CRA integration: NOT started (reviewer constraint preserved).

## Constraints honored

- Day 2 scoped EXACTLY to authenticated Core <-> worker communication
  (reviewer's do-not-start list): no streaming, no CRA, no production signing
  key, no schema migration/update, no wheelhouse/venv, no change to LightML
  1.0.1.
- WorkerManager is not redesigned; the recycle lock is a small local lock
  (D4, reviewer P0 "without redesigning WorkerManager").
- The serving Core never becomes permanently root; privileged work stays in
  the narrow supervisor/install boundary (reviewer P2).
- No external/cloud dependency — needs no Internet beyond OS dependencies;
  stdlib-only + the system `cryptography` 3.4.8 package.
- Only the TEST signing key
  `DEVELOPMENT_TEST_KEY_DO_NOT_USE_IN_PRODUCTION` exists in the repo; no
  production key created.
- LightML 1.0.1 Production: NOT modified; V2 not installed over 1.0.1; CRA
  untouched.
- Status stays: **SYDECO LIGHTML UNIVERSAL RUNTIME V2 — DEVELOPMENT /
  PROOF OF CONCEPT — AWAITING REVIEW.**

## Conclusion

The reviewer's Day-2 assignment — authenticated Core <-> worker channel — is
fully implemented and evidenced. The production recycle transition is now
race-free (one recycle under concurrent timeouts, reviewer P0); every worker
generation carries a fresh cryptographically random secret delivered ONLY via
systemd `LoadCredential=` (ephemeral root-only file, removed on stop); every
internal worker endpoint requires the Bearer credential with constant-time
comparison; credential isolation across apps and generations holds; and the
leakage sweep returns 0 secret occurrences everywhere except runtime memory
and the systemd credential mechanism. The 13-row acceptance matrix passes on
the dev equivalents AND on the real systemd path (root harness 31/31),
full suite passes 83/83 in the repo and from a fresh extraction, and the
working tree is clean after the final packaging commit. LightML 1.0.1 untouched. CRA
untouched. No production signing key created.

Status: **SYDECO LIGHTML UNIVERSAL RUNTIME V2 — DEVELOPMENT / PROOF OF
CONCEPT — AWAITING REVIEW.**
