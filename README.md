# SYDECO LightML Universal Runtime V2 — Development Repository

**Status: DEVELOPMENT / PROOF OF CONCEPT — AWAITING REVIEW.**

This is the separate development repository for the SYDECO LIGHTML
UNIVERSAL RUNTIME — VERSION 2, per the JAMALUDIN THREE-DAY ASSIGNMENT
(GeneralTask.docx, week-3). It is deliberately located OUTSIDE the
canonical LightML 1.0.1 tree so that the frozen 1.0.1 release is never
touched. LightML 1.0.1 remains CLOSED as the production baseline.

## Architecture reference (approved)

- `JAMALUDIN_Proposal_LightML_V2_Architecture_13-08-2026.docx`
  (REVISED V2.1, 2026-08-13; decisions R1..R10 locked)
- Source markdown: `Dev/Task/week-2/13-08-2026/data/report9_v2_architecture.md`

## Day 1 scope (18-08-2026)

Build the Universal Core skeleton:

- Core service, registry, manifest parser, JSON Schema validation,
  generic application routing, worker manager, health/readiness,
  Adapter interface, audit interface, CLI skeleton.
- Adapter contract: `initialize(context)`, `infer(request, context)`,
  `shutdown()` — Core knows ONLY this interface.
- Manifest schema (16 fields per GeneralTask Day 1 / proposal 4.1).
- Validation: malformed/incomplete manifests rejected (proposal 4.3/I2).
- Registry: local dev equivalent of
  `/var/lib/sydeco-lightml/registry/apps/{app_id}.json`
  (path via `SYDECO_LIGHTML_DATA_DIR`).
- Dummy application registration (Day 1 acceptance) WITHOUT editing any
  Core source file.
- 10 security tests (rejection cases).

## Day 2 scope (19-08-2026)

Implement PoC A (text-classifier) and PoC B (image-classifier) on the
SAME Core with ZERO Core source modifications between installations:

- Model loader (`loader.py`, A1/E1/E3/E5): role-based, topological
  `depends_on`, per-start sha256 re-verify.
- Internal JSON Schema subset validator (`schema.py`, H1).
- Per-app Bearer tokens (`secrets.py`, K2/R8), generated at install.
- HTTP surface (`server.py`, 5.1/5.2): single + batch infer, health
  endpoints, edge validation (H3, 1 MiB limit), output validation (H2),
  sanitized error envelope (K3/P4), K4 status taxonomy (incl. 504 M4).
  Streaming is OUT OF SCOPE Day 2 (mirrors proposal 7.1 LIMITS).
- Worker hosting (`worker.py` InProcessWorkerHost): A3 dev equivalent
  (subprocess/systemd/cgroup remain production enforcement detail),
  single-flight M3, inference timeout M4, dev-only `simulate_crash`
  test hook.
- N2 guard hardened per 19-08-2026 reviewer: symlinks resolved via
  realpath before the containment decision; security test #11
  (symlink escape) added — 11/11 security tests pass.
- PoC bundles under `examples/` (text-classifier with vectorizer+model
  `depends_on`; image-classifier with joblib model + base64 PGM input),
  each with its own manifest/model/Adapter/worker/identity/token.
- Tests: `tests/test_poc_a.py` (7), `tests/test_poc_b.py` (7),
  `tests/test_isolation.py` (2), `tests/_http_harness.py` (stdlib HTTP
  harness). Full suite: 27 tests, all PASS.
- Critical Day-2 test: Core source hashes recorded after PoC A install
  and after PoC B install — identical (ZERO Core modifications), see
  day evidence `data/evidence_p5_hash_identity.txt`.
- Isolation test: breaking PoC A leaves PoC B operational (503 vs 200).

## Day 2 correction pass (20-08-2026)

Reviewer directives (20-08-2026 review) implemented:

- All test paths are now RELATIVE to the repository location
  (`Path(__file__).resolve().parents[1]`) — no absolute
  `/home/sydeco/Dev/...` paths anywhere in `tests/` (0 occurrences).
- The oversized-payload test is now deterministic on Python 3.10 and
  3.13: it declares `Content-Length` > 1 MiB WITHOUT transmitting the
  oversized body (`tests/_http_harness.py::request_declared`) — the
  server rejects at the edge (K5/H3) with a clean 400, no
  client-side BrokenPipeError.
- The full suite (27/27) is reproduced from a FRESH extraction into
  /tmp with no dev paths present.

## Day 3 scope (20-08-2026)

PoC C + bundle security + failure testing:

- **PoC C — document triage pipeline** (`examples/document-triage/`,
  proposal 7.3/R9): the full workflow lives in the bundle's Adapter
  (preprocessing -> topic model -> rule -> sentiment model ->
  aggregation) -> structured result {topic, sentiment, urgency,
  confidence}. Three stdlib-only pickle artifacts (vectorizer + topic
  with `depends_on`, sentiment). `tests/test_poc_c.py` (7 tests).
- **Ed25519 bundle signing (R6)** — mandatory: `devkeys/` holds the
  TEST keypair
  `DEVELOPMENT_TEST_KEY_DO_NOT_USE_IN_PRODUCTION` (private 0600);
  `sydeco_lightml_core/keys.py` embeds the trusted PUBLIC key in Core
  and verifies `release.signature` FIRST at install (unsigned / wrong
  signature / unknown key_id -> rejected + audit); `tools/sign_bundle.py`
  signs the canonical manifest. All three bundles (A/B/C) are signed.
  `tests/test_signature.py` — 6 cases.
- **10 failure tests** (`tests/test_failure.py`): adapter init failure,
  adapter inference exception, inference timeout (504), worker crash,
  malformed input, output not matching output_schema, model hash
  mismatch, adapter hash mismatch, application unavailable, one app
  fails while another stays healthy. Client errors always sanitized.
- **Universal-runtime proof (PoC C)**: Core source hashes BEFORE vs
  AFTER installing/running PoC C — identical 16/16 (user sha256sum),
  see day evidence `data/evidence_p10_hash_identity_c.txt`.
- Full suite: **50 tests, all PASS**, reproduced from a fresh
  extraction (0 absolute paths; all 3 bundles' signatures verify
  against the trusted Core key).

## Constraints

- Stdlib-only (no pip install; validator + unittest). Needs no Internet
  beyond OS dependencies.
- No modification of LightML 1.0.1 source / release / installed
  artifacts. No install over 1.0.1. No CRA changes.
- No new architecture decisions outside the approved V2.1.
