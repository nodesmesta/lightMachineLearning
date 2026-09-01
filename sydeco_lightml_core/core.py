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
import hashlib
import logging
import os
import shutil
import subprocess
import sys
import zipfile
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
from .worker import (
    InProcessWorkerHost,
    SystemdTransientWorkerHost,
    WorkerManager,
    allocate_port,
)


def _resolve_within_app_root(app_root: str, *parts: str, label: str) -> str:
    """Resolve a path and reject fail-closed if it escapes the app root.

    Reapplies the N2-style containment discipline to J1/J2 dependency and venv
    paths: resolve first, then decide containment with realpath/commonpath.
    """
    root = os.path.realpath(os.path.abspath(app_root))
    path = os.path.realpath(
        os.path.abspath(os.path.normpath(os.path.join(root, *parts)))
    )
    if os.path.commonpath([root, path]) != root:
        raise RuntimeError(f"install rejected: {label} escapes app root: {path}")
    return path


class CoreService:
    """Universal LightML Core facade (Day 1 scope)."""

    def __init__(
        self,
        data_dir: Optional[str] = None,
        worker_mode: str = "inprocess",
    ) -> None:
        self.data_dir = data_dir or default_data_dir()
        self.worker_mode = worker_mode  # "inprocess" (dev/tests) | "systemd" (D1c)
        self.audit = JsonlAuditBackend(os.path.join(self.data_dir, "audit"))
        self.registry = Registry(self.data_dir, audit=self.audit)
        self.router = Router(self.registry)
        self.readiness = ReadinessStore()
        self.worker_manager = WorkerManager(readiness=self.readiness)
        self._hosts: Dict[str, Any] = {}
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
        sig_path = os.path.realpath(
            os.path.abspath(os.path.normpath(os.path.join(app_root, sig_name)))
        )
        # N2 containment — SAME rule as model/adapter artifacts
        # (reviewer finding 4, 21-08-2026): the signature file must
        # resolve STRICTLY INSIDE app_root before it is opened. A path
        # that is syntactically inside but resolves outside via
        # traversal or a symlink is REJECTED + audit.
        if not sig_path.startswith(app_root + os.sep):
            self.audit.append(
                {
                    "action": "install",
                    "app_id": manifest.get("app_id", ""),
                    "result": "fail",
                    "reason": "path traversal",
                    "artifact": sig_name,
                }
            )
            return (
                False,
                f"install rejected: signature path escapes app dir: {sig_name}",
                None,
            )
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

        # D3a / L2 (2.9): dedicated capability OS user, provisioned at
        # install time — NEVER during inference. systemd mode only
        # (requires root; the Core serving process stays non-root).
        try:
            self._provision_capability_user(app_id)
        except Exception as exc:
            self.audit.append(
                {
                    "action": "install",
                    "app_id": app_id,
                    "result": "fail",
                    "reason": "user provisioning failed",
                    "detail": str(exc),
                }
            )
            return False, f"install rejected: {exc}", None

        # Step 5: register (F1) + audit (emitted by registry)
        fs_paths: Dict[str, str] = {"app_root": app_root}
        # J1/J2 (proposal 2.4): if the app declares dependencies, build its
        # OWN venv from its local wheelhouse (offline, fail-closed) and record
        # the per-app interpreter path so the worker starts with IT (P3), not
        # the Core's interpreter. No global env is created; Core stays generic.
        try:
            fs_paths = self.build_app_venv(app_id, manifest, app_root)
        except Exception as exc:
            self.audit.append(
                {
                    "action": "install",
                    "app_id": app_id,
                    "result": "fail",
                    "reason": "environment build failed",
                    "detail": str(exc),
                }
            )
            return False, f"install rejected: {exc}", None

        entry = self.registry.register(
            app_id=app_id,
            version=version,
            manifest=manifest,
            artifact_hashes=hashes,
            filesystem_paths=fs_paths,
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

    def build_app_venv(
        self,
        app_id: str,
        manifest: Dict[str, Any],
        app_root: str,
    ) -> Dict[str, str]:
        """J1/J2 (proposal 2.4): create the application's OWN Python venv
        from its LOCAL wheelhouse only — no Internet, no global env, Core
        stays generic.

        Returns an updated ``filesystem_paths`` dict adding ``venv`` (the
        per-app interpreter directory) and ``wheelhouse`` (its local bundle
        source). ``venv`` is placed INSIDE the versioned app dir (V2.1 E2/
        R7 layout) and git-ignored, so it is not shipped in the package.

        Fail-closed (J2/P2): if the manifest declares dependencies that are
        NOT available in the app's local wheelhouse, installation fails —
        never a partial/half-valid environment, never a silently-wrong
        version. Wording (standing): needs no Internet beyond OS
        dependencies.
        """
        app_root = os.path.realpath(os.path.abspath(app_root))
        filesystem_paths: Dict[str, str] = {"app_root": app_root}
        deps = manifest.get("dependencies", [])
        wheelhouse_raw = os.path.join(app_root, "wheelhouse")
        venv_dir = _resolve_within_app_root(app_root, "venv", label="venv path")
        filesystem_paths["venv"] = venv_dir
        if os.path.lexists(wheelhouse_raw):
            wheelhouse = _resolve_within_app_root(
                app_root, "wheelhouse", label="wheelhouse path"
            )
        else:
            wheelhouse = wheelhouse_raw
        if not deps:
            # J1 requires an isolated interpreter for every application,
            # including apps with no Python dependencies. Such an app must
            # not carry undeclared dependency material in its bundle (J4).
            if os.path.isdir(wheelhouse) and any(
                fn.endswith((".whl", ".tar.gz"))
                for fn in os.listdir(wheelhouse)
            ):
                raise RuntimeError(
                    f"install rejected: dependency material present for "
                    f"dependency-free app {app_id}"
                )
            try:
                subprocess.run(
                    ["python3", "-m", "venv", venv_dir],
                    capture_output=True, text=True, check=True, timeout=120,
                )
                py = os.path.join(venv_dir, "bin", "python")
                if not os.path.isfile(py):
                    raise RuntimeError(
                        f"install rejected: venv missing interpreter: {py}"
                    )
            except BaseException:
                if os.path.isdir(venv_dir):
                    shutil.rmtree(venv_dir, ignore_errors=True)
                raise
            return filesystem_paths

        filesystem_paths["wheelhouse"] = wheelhouse
        filesystem_paths["venv"] = venv_dir

        # J4 (proposal 2.4): the wheels declared in the manifest must match
        # what is actually present in the bundle wheelhouse; mismatch REJECT.
        if not os.path.isdir(wheelhouse):
            raise RuntimeError(
                f"install rejected: no wheelhouse dir for {app_id} but "
                f"dependencies {[d['name'] for d in deps]} are declared"
            )
        declared = {(d["name"], d.get("version")) for d in deps}
        have_wheels = set()
        for fn in sorted(os.listdir(wheelhouse)):
            wp = _resolve_within_app_root(
                app_root, "wheelhouse", fn, label=f"dependency file {fn}"
            )
            # wheelhouse entries are .whl (or .tar.gz for sdist fallback).
            # Extract (name, version-ish) from the wheel filename, e.g.
            #   depballast_a-2.0.0-py3-none-any.whl  -> ('depballast-a','2.0.0')
            base = fn.split(".whl")[0] or fn.split(".tar.gz")[0]
            parts = base.split("-")
            if len(parts) >= 2:
                have_wheels.add((parts[0].replace("_", "-"), parts[1]))
        if not declared.issubset(have_wheels):
            missing = declared - have_wheels
            raise RuntimeError(
                f"install rejected: dependency not in local wheelhouse: "
                f"{sorted(missing)} (no Internet; needs no Internet beyond "
                f"OS dependencies)"
            )

        # Build the per-app venv from the app wheelhouse (offline) and install
        # ITS declared dependencies ONLY from its local wheelhouse. Any failure
        # must leave NO half-valid environment (reviewer P5).
        venv_created = False
        try:
            # Verify dependency material integrity up front (existing
            # trust/integrity architecture, J2/J4): a corrupted/mis-named wheel
            # is REJECTED here, before any venv is built.
            for fn in sorted(os.listdir(wheelhouse)):
                if fn.endswith(".whl"):
                    wp = _resolve_within_app_root(
                        app_root, "wheelhouse", fn, label=f"dependency file {fn}"
                    )
                    try:
                        with zipfile.ZipFile(wp) as zf:
                            if zf.testzip() is not None:
                                raise RuntimeError("corrupt wheel payload")
                    except (zipfile.BadZipFile, OSError, RuntimeError) as exc:
                        raise RuntimeError(
                            f"install rejected: corrupt dependency material "
                            f"{fn}: {exc}"
                        )

            # Create the per-app venv. venv creation needs only OS-dependency
            # python3-venv/ensurepip.
            subprocess.run(
                ["python3", "-m", "venv", venv_dir],
                capture_output=True, text=True, check=True, timeout=120,
            )
            venv_created = True
            py = os.path.join(venv_dir, "bin", "python")
            if not os.path.isfile(py):
                raise RuntimeError(
                    f"install rejected: venv missing interpreter: {py}"
                )
            # Install ONLY from the local wheelhouse — --no-index,
            # --find-links, no PyPI lookup, no implicit network fallback.
            proc = subprocess.run(
                [py, "-m", "pip", "install", "--no-index",
                 "--no-deps", f"--find-links={wheelhouse}",
                 *[f"{name}=={version}" for name, version in sorted(declared)]],
                capture_output=True, text=True, timeout=300,
            )
            if proc.returncode != 0:
                raise RuntimeError(
                    f"install rejected: offline pip install failed for "
                    f"{app_id}: {(proc.stderr or proc.stdout).strip()}"
                )
        except BaseException:
            # Reviewer P5: failed installation leaves NO half-valid
            # environment — remove the partially-created venv on any failure.
            if venv_created and os.path.isdir(venv_dir):
                shutil.rmtree(venv_dir, ignore_errors=True)
            raise
        return filesystem_paths

    def _provision_capability_user(self, app_id: str) -> Optional[str]:
        """D3a / L2 (2.9): create the dedicated capability OS user.

        Provisioned at install time (proposal 3.1 step 6), NEVER during
        inference; Core itself must not run permanently as root (locked
        2026-08-24). systemd mode only — returns None in inprocess mode
        (dev/tests). The app's data dir (R7a) is created and chowned to
        the user so the worker can write only its own data dir.
        """
        if self.worker_mode != "systemd":
            return None
        user = f"sydeco-cap-{app_id}"
        proc = subprocess.run(
            ["useradd", "-r", "-M", "-s", "/usr/sbin/nologin", user],
            capture_output=True, text=True,
        )
        if proc.returncode != 0 and "already exists" not in (proc.stderr or ""):
            raise RuntimeError(
                f"cannot create OS user {user}: {(proc.stderr or proc.stdout).strip()}"
            )
        app_data_dir = os.path.join(self.data_dir, "apps", app_id, "data")
        os.makedirs(app_data_dir, exist_ok=True)
        subprocess.run(
            ["chown", "-R", f"{user}:{user}", app_data_dir],
            capture_output=True, text=True,
        )
        return user

    def _start_app_systemd(
        self,
        app_id: str,
        manifest: Dict[str, Any],
        version: str,
        app_root: str,
        venv: Optional[str] = None,
    ) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        """D1c: start one app's worker as a systemd TRANSIENT unit.

        Models load INSIDE the worker process (A1), the adapter lives in
        the worker (3.0/R3); Core never imports application code in this
        mode. Core assigns the loopback port (D2 req 1), launches the
        unit with User=sydeco-cap-<id> (D3a) + cgroup/sandbox properties
        from the manifest (A3/3.3), then polls /health/ready (G1).

        J1/J2/P3: when the app owns a venv (``venv`` path from the
        registry), the worker is launched with THAT app-specific
        interpreter (``<venv>/bin/python``), NOT the Core's ``sys.executable``.
        This proves per-application Python isolation (app A uses A's
        Python, app B uses B's Python; Core never loads app deps).
        """
        app_data_dir = os.path.join(self.data_dir, "apps", app_id, "data")
        # The data dir MUST exist BEFORE systemd-run: under
        # ProtectSystem=strict, ReadWritePaths= is resolved during mount
        # namespacing — a missing path fails the unit at NAMESPACE step
        # (status 226/NAMESPACE). The dir is also chowned to the
        # capability user (D3a/R7a) so the worker can write its own data.
        os.makedirs(app_data_dir, exist_ok=True)
        os.chmod(app_data_dir, 0o755)
        if self.worker_mode == "systemd":
            try:
                subprocess.run(
                    ["chown", "-R", f"sydeco-cap-{app_id}:sydeco-cap-{app_id}",
                     app_data_dir],
                    capture_output=True, text=True, timeout=30,
                )
            except Exception:
                pass
        repo_root = os.environ.get(
            "SYDECO_LIGHTML_WORKER_REPO",
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        )
        # J1/J2/P3 (proposal 2.4): every application/version owns its own venv
        # and the worker MUST use that interpreter. Missing/broken/outside-app
        # venv paths fail closed; no silent fallback to the Core Python.
        app_root = os.path.realpath(os.path.abspath(app_root))
        if not venv:
            return False, f"start rejected: app {app_id} has no dedicated venv", None
        try:
            venv = _resolve_within_app_root(app_root, os.path.relpath(venv, app_root), label="venv path")
        except ValueError:
            return False, f"start rejected: venv path escapes app root: {venv}", None
        candidate = _resolve_within_app_root(
            app_root, os.path.relpath(os.path.join(venv, "bin", "python"), app_root),
            label="venv interpreter",
        )
        if not os.path.isfile(candidate):
            return False, f"start rejected: venv interpreter missing: {candidate}", None
        app_python = candidate
        context: Dict[str, Any] = {
            "app_root": app_root,
            "config": manifest,
            "data_dir": app_data_dir,
            "port": allocate_port(),            # D2 req 1: Core assigns
            "user": f"sydeco-cap-{app_id}",     # D3a
            "python": app_python,               # J1/J2: per-app interpreter
            "cwd": repo_root,
            "pythonpath": repo_root,
            # Day 2 (P3): Core secrets area for the ephemeral LoadCredential
            # file (root-only 0600, per launch, removed on stop — D5 #4).
            "credential_dir": os.path.join(self.data_dir, "secrets"),
        }
        host = self._hosts.get(app_id)
        if host is None:
            host = SystemdTransientWorkerHost(
                readiness=self.readiness, audit=self.audit
            )
            self._hosts[app_id] = host
            self.worker_manager.register_host(app_id, host)
        else:
            self.worker_manager.stop(app_id)
        try:
            self.worker_manager.start(app_id, version, None, context)
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

        if self.worker_mode == "systemd":
            # D1c: worker as a systemd transient unit — models load INSIDE
            # the worker (A1), adapter lives in the worker (3.0/R3).
            venv = info["filesystem_paths"].get("venv")
            return self._start_app_systemd(app_id, manifest, version, app_root, venv)

        try:
            models = load_artifacts(manifest, app_root)  # E5 at every start
        except Exception as exc:
            self.readiness.set(app_id, AppStatus.BACKOFF, detail="model load failed")
            self.audit.append(
                {"action": "worker_start", "app_id": app_id, "version": version,
                 "result": "fail", "reason": str(exc)}
            )
            return False, f"start rejected: {exc}", None

        def adapter_factory():
            return self._load_adapter(manifest, app_root)

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
            self.worker_manager.start(app_id, version, adapter_factory, context)
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

    def worker_host(self, app_id: str) -> Optional[Any]:
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
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()
