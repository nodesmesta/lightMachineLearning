"""Core service (V2.1 proposal Section 2 - skeleton).

The domain-agnostic Universal LightML Core. This module wires the
components together (registry, validation, router, worker manager,
health, audit). It knows ONLY the adapter contract (3.0) - never any
application-specific code.

Day 1: service bootstrap + install-app flow (registry + validation +
audit). HTTP serving, warm-up and the worker hosting layer are added on
later days.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Tuple

from .audit import JsonlAuditBackend
from .health import ReadinessStore
from .manifest import load_manifest
from .registry import Registry, default_data_dir
from .router import Router
from .validation import validate_manifest


class CoreService:
    """Universal LightML Core facade (Day 1 scope)."""

    def __init__(self, data_dir: Optional[str] = None) -> None:
        self.data_dir = data_dir or default_data_dir()
        self.audit = JsonlAuditBackend(os.path.join(self.data_dir, "audit"))
        self.registry = Registry(self.data_dir, audit=self.audit)
        self.router = Router(self.registry)
        self.readiness = ReadinessStore()
        self.audit.append({"action": "core_start", "app_id": "", "result": "ok"})

    def install_app(
        self,
        manifest_path: str,
        app_root: Optional[str] = None,
    ) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        """Install/register an app from its manifest.

        Returns (ok, message, registry_entry). Flow (proposal 3.1 steps
        2+4+5+7+10 for Day 1 - signature, extraction, wheelhouse and
        unit generation belong to the full installer on later days):
          1. load manifest (missing/invalid JSON -> reject)
          2. validate manifest (I2, reject malformed/incomplete)
          3. verify artifacts (E5) present + hash match
          4. register in registry (F1) + audit
        """
        # Step 1: load manifest
        try:
            manifest = load_manifest(manifest_path)
        except Exception as exc:
            self.audit.append(
                {"action": "install", "app_id": "", "result": "fail", "reason": str(exc)}
            )
            return False, f"install rejected: {exc}", None

        # Step 2: validate manifest
        ok, errors = validate_manifest(manifest)
        if not ok:
            self.audit.append(
                {
                    "action": "install",
                    "app_id": manifest.get("app_id", ""),
                    "result": "fail",
                    "reason": "manifest validation failed",
                    "errors": errors,
                }
            )
            return False, "install rejected: manifest invalid: " + "; ".join(errors), None

        app_id = manifest["app_id"]
        version = manifest["version"]

        # Step 3: verify artifacts (E5) - model files + adapter files
        app_root = os.path.abspath(app_root or os.path.dirname(manifest_path))
        hashes: Dict[str, str] = {}
        for model in manifest.get("models", []):
            rel = model.get("file", "")
            sha = model.get("sha256", "")
            full = os.path.abspath(os.path.normpath(os.path.join(app_root, rel)))
            # traversal guard (N2): artifact must stay inside app_root
            if not full.startswith(app_root + os.sep):
                self.audit.append(
                    {"action": "install", "app_id": app_id, "result": "fail",
                     "reason": "path traversal", "artifact": rel}
                )
                return False, f"install rejected: artifact path escapes app dir: {rel}", None
            if not os.path.isfile(full):
                self.audit.append(
                    {"action": "install", "app_id": app_id, "result": "fail",
                     "reason": "missing model artifact", "artifact": rel}
                )
                return False, f"install rejected: model artifact missing: {rel}", None
            hashes[rel] = _sha256_file(full)
            if hashes[rel] != sha:
                self.audit.append(
                    {"action": "install", "app_id": app_id, "result": "fail",
                     "reason": "sha256 mismatch", "artifact": rel}
                )
                return False, (
                    f"install rejected: sha256 mismatch for {rel}: "
                    f"expected {sha}, got {hashes[rel]}"
                ), None

        adapter = manifest.get("adapter", {})
        for f in adapter.get("files", []):
            rel = f.get("file", "")
            sha = f.get("sha256", "")
            full = os.path.abspath(os.path.normpath(os.path.join(app_root, rel)))
            if not full.startswith(app_root + os.sep):
                self.audit.append(
                    {"action": "install", "app_id": app_id, "result": "fail",
                     "reason": "path traversal", "artifact": rel}
                )
                return False, f"install rejected: adapter path escapes app dir: {rel}", None
            if not os.path.isfile(full):
                self.audit.append(
                    {"action": "install", "app_id": app_id, "result": "fail",
                     "reason": "missing adapter file", "artifact": rel}
                )
                return False, f"install rejected: adapter file missing: {rel}", None
            hashes[rel] = _sha256_file(full)
            if hashes[rel] != sha:
                self.audit.append(
                    {"action": "install", "app_id": app_id, "result": "fail",
                     "reason": "adapter sha256 mismatch", "artifact": rel}
                )
                return False, (
                    f"install rejected: adapter sha256 mismatch for {rel}: "
                    f"expected {sha}, got {hashes[rel]}"
                ), None

        # Step 4: E4 orphan-flag - undeclared files inside models/ are
        # flagged (audit + warning), never silently ignored, never loaded.
        declared_model_files = {
            os.path.abspath(os.path.normpath(os.path.join(app_root, m.get("file", ""))))
            for m in manifest.get("models", [])
        }
        models_dir = os.path.join(app_root, "models")
        if os.path.isdir(models_dir):
            for root, _dirs, files in os.walk(models_dir):
                for fname in files:
                    full = os.path.abspath(os.path.join(root, fname))
                    if full not in declared_model_files:
                        self.audit.append(
                            {
                                "action": "orphan_artifact",
                                "app_id": app_id,
                                "result": "flag",
                                "artifact": os.path.relpath(full, app_root),
                            }
                        )

        # Step 5: register (F1) + audit (emitted by registry)
        entry = self.registry.register(
            app_id=app_id,
            version=version,
            manifest=manifest,
            artifact_hashes=hashes,
            filesystem_paths={"app_root": os.path.abspath(app_root)},
        )
        return True, f"app registered: {app_id} v{version}", entry


def _sha256_file(path: str) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()
