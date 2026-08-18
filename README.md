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

## Constraints

- Stdlib-only (no pip install; validator + unittest). Needs no Internet
  beyond OS dependencies.
- No modification of LightML 1.0.1 source / release / installed
  artifacts. No install over 1.0.1. No CRA changes.
- No new architecture decisions outside the approved V2.1.
