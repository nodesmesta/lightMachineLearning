"""Core service (V2.1 proposal Section 2 - skeleton).

The domain-agnostic Universal LightML Core. This module wires the
components together (registry, validation, router, worker manager,
health, audit). It knows ONLY the adapter contract (3.0) - never any
application-specific code.

Day 1: service bootstrap + install-app flow (registry + validation +
audit).
Day 2: token generation at install (K2/R8), worker hosting (A3 dev
equivalent, D4), model loading (A1/E1/E3/E5) and HTTP serving (5.1/5.2)
added via start_app/stop_app/serve.
"""
from __future__ import annotations

import importlib.util
import logging
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

from .audit import JsonlAuditBackend
from .health import AppStatus, ReadinessStore
from .keys import verify_bundle_signature
from .loader import load_artifacts
from .manifest import load_manifest
from .registry import Registry, default_data_dir
from .router import Router
from .secrets import generate_token, write_token
from .validation import validate_manifest
from .worker import InProcessWorkerHost, WorkerManager


class CoreService:
    """Universal LightML Core facade (Day 1 scope)."""

    def __init__(self, data_dir: Optional[str] = None) -> None:
        self.data_dir = data_dir or default_data_dir()
        self.audit = JsonlAuditBackend(os.path.join(self.data_dir, "audit"))
        self.registry = Registry(self.data_dir, audit=self.audit)
        self.router = Router(self.registry)
        self.readiness = ReadinessStore()
        self.worker_manager = WorkerManager(readiness=self.readiness)
        self._hosts: Dict[str, InProcessWorkerHost] = {}
        self.audit.append({"action": "core_start", "app_id": "", "result": "ok"})

    def install_app(
        self,
        manifest_path: str,
        app_root: Optional[str] = None,
    ) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        """Install/register an app from its manifest.

        Returns (ok, message, registry_entry). Flow (proposal 3.1 steps
        1+2+4+5+7+10 for Day 1 - extraction, wheelhouse and unit
        generation belong to the full installer on later days):
          1. load manifest (missing/invalid JSON -> reject)
          1b. verify SYDECO signature over the canonical manifest (R6,
              Day 3 - MANDATORY: unsigned / wrong signature / unknown
              key_id -> reject + audit) BEFORE any bundle code or data
              is used
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

        # Step 1b (R6, Day 3): signature verified FIRST — before any
        # bundle code or data is used (proposal 3.1 step 1 / 3.4).
        # Signed SYDECO bundles are MANDATORY (R6).
        app_root = os.path.realpath(
            os.path.abspath(app_root or os.path.dirname(manifest_path))
        )
        release = manifest.get("release", {})
        sig_name = release.get("signature", "manifest.sig")
        sig_path = os.path.join(app_root, sig_name)
        ok_sig, sig_reason = verify_bundle_signature(manifest, sig_path)
        if not ok_sig:
            self.audit.append(
                {
                    "action": "install",
                    "app_id": manifest.get("app_id", ""),
                    "result": "fail",
                    "reason": "signature verification failed (R6)",
                    "detail": sig_reason,
                }
            )
            return False, f"install rejected: {sig_reason}", None

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
        # N2 (REVISED 2026-08-19 per reviewer): the containment decision
        # resolves filesystem symlinks (realpath) BEFORE the prefix check —
        # a path that is syntactically inside app_root but resolves outside
        # via a symlink is REJECTED.
        hashes: Dict[str, str] = {}
        for model in manifest.get("models", []):
            rel = model.get("file", "")
            sha = model.get("sha256", "")
            full = os.path.realpath(
                os.path.abspath(os.path.normpath(os.path.join(app_root, rel)))
            )
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
            full = os.path.realpath(
                os.path.abspath(os.path.normpath(os.path.join(app_root, rel)))
            )
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
            os.path.realpath(os.path.abspath(os.path.normpath(os.path.join(app_root, m.get("file", "")))))
            for m in manifest.get("models", [])
        }
        models_dir = os.path.join(app_root, "models")
        if os.path.isdir(models_dir):
            for root, _dirs, files in os.walk(models_dir):
                for fname in files:
                    full = os.path.realpath(os.path.abspath(os.path.join(root, fname)))
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
            filesystem_paths={"app_root": app_root},
        )

        # K2/R8: per-app Bearer token generated at install. Returned ONCE
        # in the CLI output (dev convenience); stored in the secrets dir
        # (mode 0600), never inside the registry file.
        token: Optional[str] = None
        if manifest.get("api", {}).get("authentication", "token") == "token":
            token = generate_token()
            write_token(self.data_dir, app_id, token)
            entry["token"] = token
        return True, f"app registered: {app_id} v{version}", entry

    # ---- Day 2: worker hosting + serving -------------------------------

    def _load_adapter(self, manifest: Dict[str, Any], app_root: str) -> Any:
        """Dynamically import the application's adapter (R3).

        Convention (dev, documented): the adapter entry module must define
        a class named ``Adapter`` implementing the 3.0 contract. Core
        never imports application types statically.
        """
        entry = manifest.get("adapter", {}).get("entry", "")
        path = os.path.join(app_root, entry)
        module_name = f"sydeco_app_{manifest.get('app_id', 'app')}_{manifest.get('version', '0')}"
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"cannot load adapter entry: {entry}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        adapter_cls = getattr(module, "Adapter", None)
        if adapter_cls is None:
            raise RuntimeError(
                f"adapter entry {entry} must define a class named 'Adapter'"
            )
        return adapter_cls()

    def start_app(self, app_id: str) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        """Start one app's worker: load models (A1/E1/E3/E5, hash
        re-verified), import + initialize the adapter (3.0/R4), mark
        ready (G2)."""
        info = self.router.resolve(app_id)
        if info is None:
            return False, f"app not registered: {app_id}", None
        manifest = info["manifest"]
        version = info["active_version"]
        app_root = info["filesystem_paths"]["app_root"]

        try:
            models = load_artifacts(manifest, app_root)  # E5 at every start
        except Exception as exc:
            self.readiness.set(app_id, AppStatus.BACKOFF, detail="model load failed")
            self.audit.append(
                {"action": "worker_start", "app_id": app_id, "version": version,
                 "result": "fail", "reason": str(exc)}
            )
            return False, f"start rejected: {exc}", None

        try:
            adapter = self._load_adapter(manifest, app_root)
        except Exception as exc:
            self.readiness.set(app_id, AppStatus.BACKOFF, detail="adapter load failed")
            self.audit.append(
                {"action": "worker_start", "app_id": app_id, "version": version,
                 "result": "fail", "reason": str(exc)}
            )
            return False, f"start rejected: {exc}", None

        # R7a: app's own writable data dir (outside the versioned layout)
        app_data_dir = os.path.join(self.data_dir, "apps", app_id, "data")
        os.makedirs(app_data_dir, exist_ok=True)
        context: Dict[str, Any] = {
            "models": models,
            "config": manifest,  # read-only by contract (3.0)
            "data_dir": app_data_dir,
            "request_id": None,
            "logger": logging.getLogger(f"sydeco-lightml.{app_id}"),
        }

        host = self._hosts.get(app_id)
        if host is None:
            host = InProcessWorkerHost(readiness=self.readiness, audit=self.audit)
            self._hosts[app_id] = host
            self.worker_manager.register_host(app_id, host)
        else:
            self.worker_manager.stop(app_id)  # clean previous lifecycle

        try:
            self.worker_manager.start(app_id, version, adapter, context)
        except Exception as exc:
            self.readiness.set(app_id, AppStatus.BACKOFF, detail="worker start failed")
            self.audit.append(
                {"action": "worker_start", "app_id": app_id, "version": version,
                 "result": "fail", "reason": str(exc)}
            )
            return False, f"start failed: {exc}", None

        self.audit.append(
            {"action": "worker_start", "app_id": app_id, "version": version,
             "result": "ok"}
        )
        return True, f"app started: {app_id} v{version}", {
            "app_id": app_id, "status": "ready", "active_version": version,
        }

    def stop_app(self, app_id: str) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        if app_id not in self._hosts:
            return False, f"app not running: {app_id}", None
        self.worker_manager.stop(app_id)
        self.audit.append({"action": "worker_stop", "app_id": app_id, "result": "ok"})
        return True, f"app stopped: {app_id}", None

    def crash_app(self, app_id: str) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        """DEV-ONLY test hook (isolation test P6): simulate a worker crash."""
        host = self._hosts.get(app_id)
        if host is None:
            return False, f"app not running: {app_id}", None
        host.simulate_crash()
        return True, f"app crashed (simulated): {app_id}", None

    def worker_host(self, app_id: str) -> Optional[InProcessWorkerHost]:
        return self._hosts.get(app_id)

    def health_apps(self) -> Dict[str, Dict[str, Any]]:
        """G4: per-app status + active version (NOT a global gate)."""
        out: Dict[str, Dict[str, Any]] = {}
        for app in self.registry.list_apps():
            app_id = app["app_id"]
            out[app_id] = {
                "status": self.readiness.status(app_id),
                "active_version": app.get("active_version"),
            }
        return out

    def api_apps(self) -> List[Dict[str, Any]]:
        """5.1: registered apps summary for GET /api/v1/apps."""
        out: List[Dict[str, Any]] = []
        for app in self.registry.list_apps():
            entry = self.registry.get(app["app_id"]) or {}
            active = app.get("active_version") or ""
            manifest = entry.get("versions", {}).get(active, {}).get("manifest", {})
            out.append({
                "app_id": app["app_id"],
                "name": manifest.get("name", ""),
                "active_version": active,
                "status": app.get("status"),
                "capabilities": manifest.get("capabilities", []),
            })
        return out

    def serve(self, host: str = "127.0.0.1", port: Optional[int] = None) -> None:
        """Start the HTTP surface (5.1). Port: SYDECO_LIGHTML_PORT env or
        the A3 default 8000."""
        from .server import CoreHTTPServer

        port = port or int(os.environ.get("SYDECO_LIGHTML_PORT", "8000"))
        httpd = CoreHTTPServer((host, port), self)
        self.audit.append(
            {"action": "server_start", "app_id": "", "result": "ok",
             "addr": f"{host}:{port}"}
        )
        httpd.serve_forever()


def _sha256_file(path: str) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()
