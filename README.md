# 31082026

## Summary

This session executes the reviewer's **PHASE 2 / DAY 3 — PER-APPLICATION
DEPENDENCY ISOLATION (J1/J2)** directive from `JAMALUDIN_DailyReport_28-08-2026
REVIEWED.docx` (review of the 28-08 submission, copied into this day's folder
`week-5/31-08-2026/`). Day-2 verdict (verbatim): **"PHASE 2 / DAY 2 —
CONDITIONALLY ACCEPTED"** (92/100; Engineering 95, Evidence 94, Packaging 84).
The reviewer's standing order: "Do not change yesterday's architecture.
Before beginning the main Day-3 work, Jamaludin must perform a short P0 Day-2
closure correction." `REMAINING WORK TO DO FROM TODAY FOR JAMALUDIN.docx`
confirms J1/J2 is today's priority ("Until each application owns its
environment and dependencies, I would not consider LightML truly autonomous").

The session is split into two parts:

1. **P0 — Day-2 closure (mandatory)** — close the reviewer's three concrete
   Day-2 defects plus the packaging that corresponds to the final commit:
   leakage regression test uses a runtime-generated credential; the ephemeral
   worker credential is removed on EVERY path (exception-safe lifecycle) with
   3 deterministic lifecycle tests; repository README updated to the
   authoritative 28-Aug state; `data/` evidence directory created and all
   evidence shipped inside the ZIP; correct `<name>.zip` + `<name>.zip.sha256`
   sidecar naming.
2. **P1..P7 — J1/J2 per-application dependency isolation** (today's core work):
   one Python venv per application/version (J1), offline wheelhouse only (J2),
   worker started with the per-app interpreter (P3), two-app incompatible-
   dependency proof (P4), failure/security tests (P5), full regression (P6),
   evidence + daily report (P7).

Status (verbatim line): **SYDECO LIGHTML UNIVERSAL RUNTIME V2 — DEVELOPMENT /
PROOF OF CONCEPT — AWAITING REVIEW.**

## P0 — Close Day 2 first (mandatory)

### P0.1 — Leakage test uses a runtime-generated secret

**File changed:** `tests/test_channel_auth_b.py`, `tests/test_channel_auth_c.py`.

**What:** `test_05_no_secret_in_manifest_registry_audit_logs` (auth_b) and
`test_04_token_absent_from_logs_manifest_registry` (auth_c) sweep the whole
repo for their test secret. They used a deterministic literal (e.g.
`secret = "99" * 32`), so the Python bytecode (`__pycache__/*.pyc`) embedded
that literal and the test reported its OWN `.pyc` as a leakage — test-harness
contamination, not a real worker-credential leak. Fixed per reviewer's
preferred correction: the credential is generated dynamically at runtime with
`secrets.token_hex(32)`. Reproducible under any interpreter / pyc state; no
`__pycache__` deletion needed.

**Verification:** `test_channel_auth_b` 7/7 PASS; `test_channel_auth_c` 6/6
PASS under a normal Python invocation with pyc present.

### P0.2 / P0.3 — Exception-safe credential cleanup + lifecycle tests

**File changed:** `sydeco_lightml_core/worker.py`; new
`tests/test_credential_lifecycle.py`.

**What:** in `SystemdTransientWorkerHost.start()` the credential file was
created before `systemd-run`; if launch or readiness failed, `_started` stayed
False and `stop()`'s `if not self._started: return` returned before unlinking
the root-only 0600 file — a failed launch could leave a credential file
behind. Now `_remove_credential()` (idempotent) is called from `stop()` before
the `_started` guard AND from `start()` on both failure paths (systemd-run
failure, readiness failure), plus a bounded `stop()` on the never-ready path
so no orphan unit remains. The reviewer-required guarantee is met verbatim:
"Every credential file must disappear after successful stop AND after every
failed launch/start path."

**Verification:** three deterministic non-privileged tests (mock subprocess) —
`test_launch_failure_removes_credential`, `test_never_ready_removes_credential`,
`test_normal_stop_removes_credential` — 3/3 PASS.

### P0.4–P0.8 — regression, evidence, packaging

- README.md = authoritative report; `data/` holds all evidence.
- Full suite in the repo after closure: **86/86 PASS** (83 prior + 3
  lifecycle); from a fresh extraction **86/86 PASS**; py_compile 17/17.
- Packaging: `SYDECO_LIGHTML_V2_DEV_2026-08-31.zip` + correctly named
  `.sha256` sidecar; verified git clean, fresh extraction PASS, no `.pyc`,
  SHA matches, evidence corresponds to final commit.

## P1 — One Python environment per application/version (J1)

**File changed:** `sydeco_lightml_core/core.py` — new `CoreService.build_app_venv()`.

**What:** installing an app that declares `dependencies` builds a **dedicated
venv for that application/version** inside its versioned dir
(`<app_root>/venv`) and records `venv`/`wheelhouse` in the registry
(`filesystem_paths`). Resolution is per-app (M1/C3): `app_id + version ->
application files + models + dedicated venv + worker`. No second/global Python
environment is created and the Core remains generic (it never imports an
application's layout or dependencies). Registry records the resolved per-version
paths; the worker resolves its interpreter from the registry.

## P2 — Offline wheelhouse only (J2)

**What:** installation builds the app venv **exclusively** from the app's OWN
local wheelhouse (`<app_root>/wheelhouse`) via
`pip install --no-index --no-deps --find-links=<wheelhouse> <spec>` — no PyPI,
no implicit network fallback, no dependency silently taken from the Core
environment. Dependencies absent / corrupt / version-mismatched are REJECTED
(fail-closed), leaving no partial environment. Wheel material is verified
up-front (zipfile integrity, bundle-wheel match, J4). Wording (standing):
needs no Internet beyond OS dependencies.

## P3 — Worker uses the application-specific interpreter

**File changed:** `sydeco_lightml_core/core.py` (`_start_app_systemd`).

**What:** the systemd context previously set `"python": sys.executable` (the
Core's interpreter). It now resolves the per-app interpreter from the registry
venv (`<app_root>/venv/bin/python`) when one exists, so Application A's worker
starts with A's Python and Application B's worker with B's Python. The Core
does not load the application's dependencies.

## P4 — Proven real dependency isolation (central acceptance)

**Proof (new `tests/test_dep_isolation.py`):** two deterministic apps both
depend on the stdlib-only fixture package `depballast` at INCOMPATIBLE
versions — App A -> 1.0.0, App B -> 2.0.0 — each wheel built offline and placed
in each app's own `wheelhouse/`. Proved simultaneously:

| App | declared | installed | interpreter | behavior() | VERSION |
|-----|----------|-----------|-------------|------------|---------|
| app-a | depballast 1.0.0 | 1.0.0 | `<app-a>/venv/bin/python` | '1.0.0-behavior' | 1.0.0 |
| app-b | depballast 2.0.0 | 2.0.0 | `<app-b>/venv/bin/python` | '2.0.0-behavior' | 2.0.0 |

- Both return valid results (behavior reflects its own version).
- Distinct per-app interpreters (separate venvs).
- **Restart stability:** breaking/removing App A's venv does NOT affect App B
  (B still imports/serves 2.0.0); installing/updating A cannot alter B's
  environment.
- **Neither modifies the Core environment:** the global/system python cannot
  `import depballast` (it exists only inside the per-app venvs).

## P5 — Failure and security tests

`tests/test_dep_isolation.py` failure matrix (reviewer P5 minimum set) — all
fail CLOSED with no half-valid environment:

| Case | Result |
|------|--------|
| missing dependency wheel | REJECT |
| wrong/unavailable dependency version | REJECT |
| corrupt dependency material | REJECT (verified before venv; no half env) |
| App A dependency failure | App B remains serving its own version |
| incomplete venv creation | no application marked READY |
| no Internet available | valid offline install still succeeds |
| worker uses per-app interpreter | confirmed |
| dependency paths cannot escape allowed area | by construction (inside app_root) |
| dependency name/path shell-injection | no command executed (ARGV, not shell) |
| failed install leaves no registry entry / half env | no falsely-active entry; partial venv removed |

## P6 — Regression

| Check | Result |
|-------|--------|
| Full suite IN THE REPO | **91/91 PASS** (86 prior + 5 new J1/J2) |
| Full suite FROM FRESH ZIP EXTRACTION | **91/91 PASS** |
| py_compile core modules | 17/17 |
| dev-path occurrences in source (core/tests/examples) | 0 |
| git working tree | clean (HEAD `f64042c`) |
| LightML 1.0.1 / CRA / production key | untouched / untouched / not created |

## P7 — Evidence and daily report

- `data/evidence_day3_j12_venv_isolation.txt` — J1/J2 venv/wheelhouse
  architecture paths, offline mechanism, commands, App A/B incompatible-
  dependency proof.
- `data/evidence_day3_failure_matrix.txt` — reviewer P5 failure/security
  matrix.
- This README.md (English, single source of truth).
- `Dev/Report/week-5/JAMALUDIN_DailyReport_31-08-2026.docx` (generated from
  this README).
- `SYDECO_LIGHTML_V2_DEV_2026-08-31.zip` + `.sha256` sidecar containing
  source + `.git` + tests + README + ALL `data/` evidence; verified fresh
  extraction PASS, no `.pyc`, SHA matches.

## Bugs / architecture issues found and fixed (2026-08-31)

| # | Issue (root cause) | Fix |
|---|--------------------|-----|
| 1 | (reviewer P0-1) leakage test used a deterministic literal secret, so compiled bytecode reported itself as a leakage | runtime `secrets.token_hex(32)` in the two leak-scan tests |
| 2 | (reviewer P0-2) failed launch could leave the ephemeral root-only 0600 credential file because `stop()` returned before unlinking when `_started` was False | idempotent `_remove_credential()` called from `stop()` before the guard and from `start()` on both failure paths; 3 lifecycle tests |
| 3 | (packaging) the delivered ZIP had no `data/` evidence because `data/` was git-ignored | track `data/evidence_*.txt` (keep `data/registry/` ignored) so the package corresponds to the final commit |
| 4 | (J1/J2 gap) worker used the Core's `sys.executable`, not a per-app interpreter | `build_app_venv()` + registry venv path + per-app interpreter resolution (P1/P2/P3) |

## Verification (before declaring done)

| Check | Result |
|-------|--------|
| P0 closure focused (auth_b / auth_c / lifecycle) | 7/7 / 6/6 / 3/3 PASS |
| J1/J2 focused (`test_dep_isolation`) | 5/5 PASS |
| FULL suite IN THE REPO | 91/91 PASS |
| Full suite FROM FRESH ZIP EXTRACTION | 91/91 PASS, 0 dev-path, py_compile 17/17 |
| Leakage sweep | 0 occurrences |
| 0 `__pycache__` / `.pyc` in deliverable | PASS |
| git commit + clean tree | 3 commits; clean |
| Evidence files secret-free | PASS |
| LightML 1.0.1 / CRA / production key | untouched / untouched / not created |

## Evidence table

| File | Content |
|------|---------|
| `data/evidence_p1_recycle_concurrency.txt` | Day-2 P1 (concurrent-timeout recycle) |
| `data/evidence_p2_p3_secret_delivery.txt` | Day-2 P2/P3 (per-generation secret, LoadCredential) |
| `data/evidence_p4_auth_endpoints.txt` | Day-2 P4 (Bearer on all worker endpoints) |
| `data/evidence_p5_credential_isolation.txt` | Day-2 P5 (App A vs App B, old vs new gen) |
| `data/evidence_p6_leakage.txt` | Day-2 P6 (0 occurrences) |
| `data/evidence_p7_acceptance_matrix.txt` | Day-2 P7 non-privileged (13-row matrix) |
| `data/evidence_p7_root_acceptance.txt` | Day-2 P7 ROOT real-systemd (31/31) |
| `data/evidence_p8_fresh_extraction.txt` | Day-2 P8 fresh extraction |
| `data/evidence_regression_full_suite.txt` | Day-2 P8 full suite in repo |
| `data/evidence_day3_j12_venv_isolation.txt` | **NEW** Day-3 J1/J2 venv isolation + App A/B proof |
| `data/evidence_day3_failure_matrix.txt` | **NEW** Day-3 P5 failure/security matrix |
| `data/SYDECO_LIGHTML_V2_DEV_2026-08-31.zip` + `.sha256` | Day-3 package: source + .git + tests + README + all evidence |

## What remains

- The reviewer's verification of this Day-3 report + package.
- Phase 2 hardening continues (reviewer's agreed order): streaming (K5),
  update handling + schema migration (F4/F6), production signing-key
  infrastructure (R6 infra), permanent systemd units/full installer.
- CRA integration: NOT started (reviewer constraint preserved).

## Constraints honored

- Day-3 scoped EXACTLY to J1/J2 dependency isolation + P0 closure (reviewer's
  do-not-start list): no streaming/K5, no CRA, no schema migration F4/F6, no
  production signing key, no permanent systemd unit, no change to LightML
  1.0.1.
- No external/cloud dependency — needs no Internet beyond OS dependencies;
  stdlib-only + the system `cryptography` 3.4.8 package (the one permitted
  exception); no pip installs against the Internet (offline wheelhouse only).
- Tests/experiments only in isolated areas (/tmp + the dev repo); no global
  Python environment created; venv/wheelhouse never committed.
- Only the TEST signing key exists; no production signing key created.
- Wording discipline (standing): "needs no Internet beyond OS dependencies".

## Conclusion

The reviewer's Day-2 closure items are complete (runtime leakage secret,
exception-safe credential cleanup with 3 lifecycle tests, evidence inside the
package, correct sidecar name). Day-3 J1/J2 delivers the central acceptance:
each application owns its Python environment and dependencies, isolated from
every other application and from the Core, built entirely offline from its own
local wheelhouse, and launched under its own interpreter. Full suite passes
91/91 in the repo and from a fresh extraction; the working tree is clean after
the three session commits. LightML 1.0.1 untouched. CRA untouched. No
production signing key created.

Status: **SYDECO LIGHTML UNIVERSAL RUNTIME V2 — DEVELOPMENT / PROOF OF
CONCEPT — AWAITING REVIEW.**