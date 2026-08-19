"""Model loader (V2.1 proposal 2.1 / A1 / E1, E3, E5).

Core-provided loader for the E1 format whitelist. Artifacts are loaded by
ROLE (E3): the manifest declares roles and optional depends_on; Core loads
in dependency order (topological) and rejects dependency cycles at
install/load. E5: per-artifact sha256 is re-verified at EVERY worker
start; a mismatch refuses the start (readiness stays not_ready) + audit
event.

Formats (E1 baseline V2): pickle and joblib. A joblib artifact is a
pickle-compatible file (we only ever load the plain-dict artifacts our
own PoC bundles ship; arbitrary deserialization trust is governed by the
bundle verification chain R5/R6 — signature enforcement is Day 3).

Loading happens INSIDE the capability worker process (A1). Custom loader
code shipped by a capability is rejected for the V2 baseline (would be
arbitrary code execution) — loaders are Core-provided only.
"""
from __future__ import annotations

import hashlib
import os
import pickle
from typing import Any, Dict, List, Optional, Set, Tuple


class LoadError(Exception):
    """Raised when artifacts cannot be loaded or verified."""


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _load_pickle(path: str) -> Any:
    with open(path, "rb") as fh:
        return pickle.load(fh)


# E1: Core-provided loaders, keyed by the manifest `format` field.
LOADERS = {
    "pickle": _load_pickle,
    "joblib": _load_pickle,  # joblib files are pickle-compatible (stdlib path)
    # "torch" is whitelisted in E1 but requires the torch package; it is
    # NOT available in the stdlib-only dev environment. A bundle declaring
    # torch is rejected at install by validation.py unless torch exists
    # (see validation E1 whitelist handling in core.py install flow).
}


def verify_artifact_hashes(manifest: Dict[str, Any], app_root: str) -> Dict[str, str]:
    """E5: verify every declared artifact exists and matches its sha256.

    Returns {relative_path: sha256} on success; raises LoadError on the
    first mismatch (message includes artifact + expected/actual).
    """
    hashes: Dict[str, str] = {}
    for model in manifest.get("models", []):
        rel = model.get("file", "")
        sha = model.get("sha256", "")
        full = os.path.abspath(os.path.join(app_root, rel))
        if not os.path.isfile(full):
            raise LoadError(f"model artifact missing: {rel}")
        actual = _sha256_file(full)
        if actual != sha:
            raise LoadError(
                f"sha256 mismatch for {rel}: expected {sha}, got {actual}"
            )
        hashes[rel] = actual
    for f in manifest.get("adapter", {}).get("files", []):
        rel = f.get("file", "")
        sha = f.get("sha256", "")
        full = os.path.abspath(os.path.join(app_root, rel))
        if not os.path.isfile(full):
            raise LoadError(f"adapter file missing: {rel}")
        actual = _sha256_file(full)
        if actual != sha:
            raise LoadError(
                f"adapter sha256 mismatch for {rel}: expected {sha}, got {actual}"
            )
        hashes[rel] = actual
    return hashes


def load_artifacts(manifest: Dict[str, Any], app_root: str) -> Dict[str, Any]:
    """E3+E5: verify hashes, resolve depends_on topologically, load by role.

    Returns {role: loaded_object}. Raises LoadError on any failure
    (missing artifact, hash mismatch, unknown format, dependency cycle,
    unknown dependency role).
    """
    verify_artifact_hashes(manifest, app_root)

    models = manifest.get("models", [])
    by_role: Dict[str, Dict[str, Any]] = {}
    for m in models:
        role = m.get("role")
        if not role or role in by_role:
            raise LoadError(f"duplicate or empty model role: {role!r}")
        by_role[role] = m

    # Topological order (Kahn): role -> set of roles it depends on.
    dependents: Dict[str, Set[str]] = {r: set() for r in by_role}
    for role, m in by_role.items():
        dep = m.get("depends_on")
        if dep:
            if dep not in by_role:
                raise LoadError(
                    f"model role {role!r} depends on unknown role {dep!r}"
                )
            dependents[dep].add(role)

    # Build adjacency: dep_role -> [roles that depend on it]
    adj: Dict[str, List[str]] = {r: [] for r in by_role}
    indegree: Dict[str, int] = {r: 0 for r in by_role}
    for role, m in by_role.items():
        dep = m.get("depends_on")
        if dep:
            adj[dep].append(role)
            indegree[role] += 1

    ready = [r for r, d in indegree.items() if d == 0]
    order: List[str] = []
    while ready:
        r = ready.pop(0)
        order.append(r)
        for nxt in adj[r]:
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                ready.append(nxt)
    if len(order) != len(by_role):
        raise LoadError("model dependency cycle detected (E3)")

    loaded: Dict[str, Any] = {}
    for role in order:
        m = by_role[role]
        fmt = m.get("format")
        loader = LOADERS.get(fmt)
        if loader is None:
            raise LoadError(
                f"model {role!r}: format {fmt!r} not loadable in this "
                f"environment (E1 whitelist: {sorted(LOADERS)})"
            )
        full = os.path.abspath(os.path.join(app_root, m.get("file", "")))
        loaded[role] = loader(full)
    return loaded
