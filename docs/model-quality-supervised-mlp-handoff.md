# Supervised MLP Candidate Handoff

Date: 16 November 2026
Status: Candidate runtime verified; production promotion blocked.

## Scope

This change adds one dependency-free supervised LightML candidate while preserving the existing 12-feature input contract, adapter contract, fail-closed behavior, and production bundle.

The candidate is not a hybrid scorer. The existing Isolation Forest production artifact remains unchanged and is not combined with the candidate.

## Implementation

- `bundles/socsoar-alert-scoring/models/supervised_mlp.py`
  - 12 -> 32 -> 16 -> 1 MLP;
  - ReLU hidden layers and sigmoid output;
  - training-partition standardization stored in the artifact;
  - deterministic seed, class-weighted loss, L2 regularization, bounded training;
  - `predict_proba()` compatibility with the existing adapter.
- `tools/train_supervised_mlp.py`
  - trains only from the frozen reviewed train partition;
  - records dataset/model hashes, seed, architecture, and preprocessing identity.
- `tools/evaluate_supervised_mlp.py`
  - selects threshold from validation only;
  - evaluates the frozen candidate once on the untouched test partition.
- `sydeco_lightml_core/loader.py`
  - resolves pickled model modules from the canonical bundle `models/` directory;
  - removes path-order ambiguity from duplicate model modules.

## Candidate evidence

Evidence is stored in the week-16 task workspace:

`/home/sydeco/Dev/Task/2026-11/week-16/16/data/model-quality/`

Candidate model SHA-256:

`264136e1b761e333922dca44bd8cf6ff717617066cff82561913613b21ad334b`

Fresh cohort SHA-256:

`593f265a458a5b1f0f6ce4b6da3b1f3e038996f453c34daf8f5d43b3710af557`

Frozen evaluation result:

- validation-only threshold: `0.5`;
- test accuracy: `1.00`;
- balanced accuracy: `1.00`;
- confusion matrix: `TN=20, FP=0, FN=0, TP=20`;
- FPR/FNR: `0.00`;
- `model_fit=false` during final evaluation.

## Runtime verification

The disposable signed candidate bundle passed the native connector qualification:

- 91/91 cases passed;
- signature-verified installation;
- valid inference and receipt verification;
- invalid input/authentication fail closed with no score;
- worker outage returns `unavailable` with no score;
- worker restart recovers.

Regression suites:

- LightML: 229 passed;
- Project3 with `PYTHONPATH=src`: 376 passed, 39 environment-dependent skips.

## Decision

`DO NOT PROMOTE`.

The result is controlled laboratory evidence only. Independent human verification, broader acceptance evidence, and resolution of the historical production training provenance remain open. The production model artifact and production bundle are intentionally unchanged.
