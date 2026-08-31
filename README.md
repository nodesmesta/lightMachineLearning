# SYDECO LIGHTML UNIVERSAL RUNTIME V2 — DEVELOPMENT / PROOF OF CONCEPT — AWAITING REVIEW.

**Phase 2 / Day 3 — Per-Application Dependency Isolation (J1/J2) — 2026-08-31.**

Status line (verbatim): **SYDECO LIGHTML UNIVERSAL RUNTIME V2 — DEVELOPMENT /
PROOF OF CONCEPT — AWAITING REVIEW.**

Final statement (required): **LightML 1.0.1 untouched. CRA untouched. No
production signing key created.**

## Summary

This session executes the reviewer's **PHASE 2 / DAY 3 — PER-APPLICATION
DEPENDENCY ISOLATION (J1/J2)** directive from `JAMALUDIN_DailyReport_28-08-2026
REVIEWED.docx` (Day-2 verdict: **CONDITIONALLY ACCEPTED**; P0 Day-2 closure
mandatory before Day-3). The day is split into two parts:

1. **P0 — Day-2 closure (mandatory)** — the three concrete Day-2 defects the
   reviewer required corrected before Day-3, plus packaging that corresponds
   to the final commit:
   - leakage regression test fixed to use a runtime-generated credential
     (`secrets.token_hex(32)`) so compiled bytecode can never be a false
     leak;
   - ephemeral worker credential now removed on EVERY path (successful stop
     and all failed launch/start paths) via an exception-safe lifecycle,
     with 3 deterministic lifecycle tests;
   - repository README updated to the authoritative 28-Aug state;
   - `data/` evidence directory created and all evidence shipped inside the
     ZIP (it was previously missing because `data/` was git-ignored);
   - correct `<name>.zip` + `<name>.zip.sha256` sidecar naming.
2. **P1..P7 — J1/J2 per-application dependency isolation** (today's core
   work).

## P0 — Day-2 closure (mandatory)

### P0.1 — Leakage test uses a runtime-generated secret
**File changed:** `tests/test_channel_auth_b.py`, `tests/test_channel_auth_c.py`.
`test_05_no_secret_in_manifest_registry_audit_logs` (auth_b) and
`test_04_token_absent_from_logs_manifest_registry` (auth_c) both sweep the
whole repository for their test secret. They previously used a deterministic
literal (e.g. `secret = "99" * 32`), so the Python bytecode
(`__pycache__/*.pyc`) embedded that literal and the test reported its OWN
`.pyc` as a leakage — test-harness contamination, not a real worker-credential
leak. Fixed per reviewer's preferred correction: generate the credential
dynamically with `secrets.token_hex(32)`. Reproducible under any interpreter /
pyc state, no `__pycache__` deletion needed. Verified: `test_channel_auth_b`
7/7, `test_channel_auth_c` 6/6.

### P0.2/P0.3 — Exception-safe credential cleanup + lifecycle tests
**File changed:** `sydeco_lightml_core/worker.py`, new `tests/test_credential_lifecycle.py`.
In `SystemdTransientWorkerHost.start()` the credential file was created before
`systemd-run`; if launch/readiness failed, `_started` stayed False and `stop()`'
s `if not self._started: return` returned before unlinking the root-only 0600
file — a failed launch could leave a credential file behind. Now:
`_remove_credential()` (idempotent) is called from `stop()` BEFORE the
`_started` guard AND from `start()` on both failure paths (systemd-run failure,
readiness failure), plus a bounded `stop()` on the never-ready path so no
orphan unit remains. All three reviewer-required cases have deterministic
non-privileged tests (mock subprocess — no root/systemd needed):
`test_launch_failure_removes_credential`, `test_never_ready_removes_credential`,
`test_normal_stop_removes_credential` — 3/3.
"Every credential file must disappear after successful stop AND after every
failed launch/start path."

### P0.4–P0.8 — Regression, evidence, packaging
- README.md (this file) = authoritative report. `data/` holds ALL evidence
  (Day-2 nine files + Day-3 evidence).
- Full suite 86/86 in repo and fresh extraction after closure (83 prior + 3
  lifecycle).
- Packaging: `SYDECO_LIGHTML_V2_DEV_2026-08-31.zip` (source + .git + tests +
  README + all `data/` evidence; no `__pycache__`/`.pyc`) + correctly named
  `.sha256` sidecar. Verified: git clean, fresh-extraction PASS, SHA matches,
  evidence corresponds to final commit.

## P1 — One Python environment per application/version (J1)

**File changed:** `sydeco_lightml_core/core.py` — new `CoreService.build_app_venv()`.
Installing an app that declares `dependencies` builds a **dedicated venv for
that application/version** inside its versioned dir (`<app_root>/venv`) and
records `venv`/`wheelhouse` in the registry (`filesystem_paths`). Resolution
is per-app (M1/C3): `app_id + version -> application files + models +
dedicated venv + worker`. No second/global Python environment is created and
the Core remains generic (it never imports an application's layout or deps).

## P2 — Offline wheelhouse only (J2)

Installation builds the app venv **exclusively** from the app's OWN local
wheelhouse (`<app_root>/wheelhouse`) via
`pip install --no-index --no-deps --find-links=<wheelhouse> <spec>` — no PyPI,
no implicit network fallback, no dependency silently taken from the Core
environment. Dependencies absent/corrupt/version-mismatched are REJECTED
(fail-closed), leaving no partial environment. Wheel material is verified
up-front (zipfile integrity, J4 bundle-wheel match). Wording (standing):
needs no Internet beyond OS dependencies.

## P3 — Worker uses the application-specific interpreter

**File changed:** `sydeco_lightml_core/core.py` (`_start_app_systemd`).
The systemd context previously set `"python": sys.executable` (the Core's
interpreter). It now resolves the per-app interpreter from the registry venv
(`<app_root>/venv/bin/python`) when one exists, so Application A's worker
starts with A's Python and Application B's worker with B's Python. The Core
does not load the application's dependencies.

## P4 — Proven real dependency isolation (central acceptance)

**Proof (new `tests/test_dep_isolation.py`, 5 tests):** two deterministic
apps both depend on the stdlib-only fixture package `depballast` at
INCOMPATIBLE versions — App A -> 1.0.0, App B -> 2.0.0 — each wheel built
offline and placed in each app's own `wheelhouse/`. Proved simultaneously:

| App | declared | installed | interpreter | behavior() | VERSION |
|-----|----------|-----------|-------------|------------|---------|
| app-a | depballast 1.0.0 | 1.0.0 | `<app-a>/venv/bin/python` | '1.0.0-behavior' | 1.0.0 |
| app-b | depballast 2.0.0 | 2.0.0 | `<app-b>/venv/bin/python` | '2.0.0-behavior' | 2.0.0 |

- Both return valid results (behavior reflects its own version).
- Distinct per-app interpreters (separate venvs).
- **Restart stability:** breaking/removing App A's venv does NOT affect App B
  (B still imports and serves 2.0.0). Installing/updating A cannot alter B's
  environment (fully separate venvs/wheelhouses).
- **Neither modifies the Core Python environment:** the global/system python
  cannot `import depballast` (it exists only inside the per-app venvs).

## P5 — Failure and security tests

`tests/test_dep_isolation.py` failure matrix (reviewer P5 minimum set) — all
fail CLOSED with no half-valid environment:

| Case | Result |
|------|--------|
| missing wheel | REJECT |
| wrong/unavailable dependency version | REJECT |
| corrupt dependency material | REJECT (verified before venv, no half env) |
| App A dependency failure | App B remains serving its own version |
| incomplete venv creation | no application marked READY |
| no Internet available | valid offline install still succeeds |
| worker uses per-app interpreter | confirmed |
| dependency paths cannot escape allowed area | by construction (inside app_root) |
| dependency name/path shell-injection | no command executed (subprocess ARGV, not shell) |
| failed install leaves no registry entry / half env | no falsely-active entry; partial venv removed |

## P6 — Regression

Full suite in the repo: **91 tests -> OK** (86 prior + 5 new J1/J2).
From a fresh extraction: **91 tests -> OK**. `py_compile` core: **17/17**.
Dev-path occurrences in source (core/tests/examples): **0**. Git tree clean.
LightML 1.0.1 untouched; CRA untouched; no production signing key.

## P7 — Evidence and daily report

- `data/evidence_day3_j12_venv_isolation.txt` — J1/J2 venv/wheelhouse
  architecture paths, offline mechanism, commands, App A/B incompatible-
  dependency proof table.
- `data/evidence_day3_failure_matrix.txt` — reviewer P5 failure/security
  matrix.
- This README.md (English, single source of truth).
- `Dev/Report/week-5/JAMALUDIN_DailyReport_31-08-2026.docx` (generated from
  this README).
- `SYDECO_LIGHTML_V2_DEV_2026-08-31.zip` + `.sha256` sidecar containing
  source + `.git` + tests + README + ALL `data/` evidence, verified fresh
  extraction PASS, no `.pyc`, SHA matches.

## Evidence files shipped in the ZIP

- data/evidence_p1_recycle_concurrency.txt
- data/evidence_p2_p3_secret_delivery.txt
- data/evidence_p4_auth_endpoints.txt
- data/evidence_p5_credential_isolation.txt
- data/evidence_p6_leakage.txt
- data/evidence_p7_acceptance_matrix.txt
- data/evidence_p7_root_acceptance.txt
- data/evidence_p8_fresh_extraction.txt
- data/evidence_regression_full_suite.txt
- data/evidence_day3_j12_venv_isolation.txt
- data/evidence_day3_failure_matrix.txt

## Constraints honored

- Do-not-start list followed: no streaming/K5, no CRA, no schema migration
  F4/F6, no production signing-key infrastructure, no permanent systemd unit,
  no change to LightML 1.0.1.
- No external/cloud dependency — needs no Internet beyond OS dependencies;
  stdlib-only + the system `cryptography` 3.4.8 (the one permitted exception);
  no pip installs against the Internet (offline wheelhouse only).
- Tests/experiments only in isolated areas (/tmp + the dev repo); no global
  Python env created; venv/wheelhouse never committed.
- Only the TEST signing key exists; no production signing key created.
- Wording discipline: "needs no Internet beyond OS dependencies".

## Conclusion

The Day-2 defects the reviewer required are closed (runtime leakage secret,
exception-safe credential cleanup, evidence inside the package, correct
sidecar name). Day-3 J1/J2 proves the central acceptance: each application
owns its Python environment and dependencies, isolated from every other
application and from the Core, built entirely offline from its own local
wheelhouse, and launched under its own interpreter. Full suite 91/91 in repo
and fresh extraction; git tree clean. LightML 1.0.1 untouched. CRA untouched.
No production signing key created.

Status: **SYDECO LIGHTML UNIVERSAL RUNTIME V2 — DEVELOPMENT / PROOF OF
CONCEPT — AWAITING REVIEW.**