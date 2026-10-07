"""Worker runtime (V2.1 proposal 2.3/A3, 2.1/A1, 2.2/G1, 2.11/M3/M4,
3.0/R1-R4) — runs INSIDE the capability worker process.

Launched by the Core supervisor as a systemd transient unit (D1c, user
decision 2026-08-24). Responsibilities, all inside THIS process:

- Bind 127.0.0.1 ONLY (L3; the host is hard-coded, there is no code path
  that binds 0.0.0.0 / :: / an external interface — see the bind
  security test, acceptance 8).
- Load models INSIDE the worker via the Core loader (A1: memory
  accounting stays under the A3 cgroup; E1/E3/E5: format whitelist,
  role/depends_on resolution, per-artifact sha256 re-verified at every
  start).
- Import the bundle adapter (3.0/R3) and run adapter.initialize (R4)
  BEFORE serving; init failure -> process exits non-zero -> the unit
  fails -> crash-loop backoff (A3) at the supervisor.
- Worker-internal HTTP surface (G1, called only by Core on loopback):
      GET  /health/live   -> process is up
      GET  /health/ready  -> models loaded + adapter.initialize done
      POST /infer         -> single request, single-flight (M3);
                             the body is the ALREADY edge-validated
                             input object; the reply is
                             {"result": <adapter result>} — output
                             validation (H2) happens at the Core edge
                             before the B1 envelope.
- Inference timeout (M4) is enforced by CORE (client-side HTTP timeout
  -> 504 + worker termination). The worker itself stays single-flight
  and simple.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from . import loader
from .network_policy import adapter_network_guard

log = logging.getLogger("sydeco-lightml.worker-runtime")

# L3: fixed loopback bind. There is intentionally NO --host option.
WORKER_BIND_HOST = "127.0.0.1"


def load_worker_secret(credential_file: Optional[str]) -> str:
    """Day 2 (P3): read the worker credential — the ONLY delivery paths.

    1. systemd LoadCredential= -> $CREDENTIALS_DIRECTORY/worker-secret
       (the production path; the source file is root-only and is never
       visible to the worker — systemd injects it into the unit).
    2. --credential-file <path> (explicit dev/test approximation: the
       PATH may appear on the command line, the SECRET never does).

    Fail-closed: no credential -> WorkerRuntimeError -> the unit fails
    (there is deliberately NO unauthenticated worker mode).
    """
    secret: Optional[str] = None
    cred_dir = os.environ.get("CREDENTIALS_DIRECTORY")
    if cred_dir:
        path = os.path.join(cred_dir, "worker-secret")
        try:
            with open(path, "r", encoding="utf-8") as fh:
                secret = fh.read().strip()
        except OSError:
            secret = None
    if not secret and credential_file:
        try:
            with open(credential_file, "r", encoding="utf-8") as fh:
                secret = fh.read().strip()
        except OSError:
            secret = None
    if not secret:
        raise WorkerRuntimeError(
            "no worker credential configured "
            "(CREDENTIALS_DIRECTORY/worker-secret or --credential-file)"
        )
    return secret


class WorkerRuntimeError(Exception):
    """Worker failed to initialize (exit non-zero -> unit fails)."""


def _audit_auth_failure(runtime: Any, source: str) -> None:
    """Append an AUTH_FAILURE event to the worker's OWN audit JSONL (C2).

    The Core cannot observe 401s raised inside another process, so the
    worker records auth failures itself in its writable data dir
    (``<data_dir>/audit/audit.jsonl``). The event never contains the
    credential or the presented token — only the source endpoint.
    """
    try:
        data_dir = getattr(runtime, "data_dir", None)
        if not data_dir:
            return
        audit_dir = os.path.join(data_dir, "audit")
        os.makedirs(audit_dir, exist_ok=True)
        entry = {
            "action": "AUTH_FAILURE",
            "app_id": getattr(runtime, "app_id", "?"),
            "result": "rejected",
            "detail": f"invalid bearer on {source}",
            "timestamp": _now_iso(),
        }
        with open(
            os.path.join(audit_dir, "audit.jsonl"), "a", encoding="utf-8"
        ) as fh:
            fh.write(json.dumps(entry, sort_keys=True) + "\n")
    except Exception:
        # audit must never break serving
        pass


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _check_bearer(runtime: Any, presented: Optional[str]) -> bool:
    """Constant-time comparison of the presented Bearer (P4).

    Requires ``Authorization: Bearer <token>``; ``hmac.compare_digest``
    against the worker's loaded secret. No unauth'd worker endpoint.
    """
    import hmac

    if not presented:
        return False
    secret = getattr(runtime, "secret", None)
    if not secret:
        return False
    return hmac.compare_digest(presented.encode("utf-8"), secret.encode("utf-8"))


def load_adapter(manifest: Dict[str, Any], app_root: str) -> Any:
    """Import the bundle adapter (3.0/R3) — same convention as Core.

    If the manifest declares no adapter, fallback to DefaultModelAdapter (zero-code mode).
    Otherwise, dynamically import the declared Adapter class.
    """
    adapter_decl = manifest.get("adapter")
    if not adapter_decl or not adapter_decl.get("entry"):
        from .adapter import DefaultModelAdapter
        return DefaultModelAdapter()

    entry = adapter_decl.get("entry", "")
    path = os.path.join(app_root, entry)
    module_name = f"sydeco_worker_{manifest.get('app_id', 'app')}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise WorkerRuntimeError(f"cannot load adapter entry: {entry}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    adapter_cls = getattr(module, "Adapter", None)
    if adapter_cls is None:
        raise WorkerRuntimeError(
            f"adapter entry {entry} must define a class named 'Adapter'"
        )
    return adapter_cls()


def build_context(
    manifest: Dict[str, Any], models: Dict[str, Any], data_dir: Optional[str]
) -> Dict[str, Any]:
    """Strictly bounded adapter context (3.0/R1): read-only except the
    app's own data dir."""
    return {
        "models": models,
        "config": manifest,
        "data_dir": data_dir,
        "request_id": None,
        "logger": logging.getLogger(f"sydeco-lightml.{manifest.get('app_id', 'app')}"),
    }


class WorkerHandler(BaseHTTPRequestHandler):
    """Worker-internal HTTP handler (loopback only, G1/M3)."""

    protocol_version = "HTTP/1.1"
    server_version = "sydeco-lightml-worker/2.0-dev"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        log.debug("worker http: " + format % args)

    def _send_json(self, code: int, obj: Any) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_stream_frame(self, frame: Dict[str, Any]) -> None:
        self.wfile.write(json.dumps(frame).encode("utf-8") + b"\n")
        self.wfile.flush()

    def _error(self, code: int, message: str) -> None:
        self._send_json(code, {"error": {"code": str(code), "message": message}})

    # ---- GET -----------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        runtime = getattr(self.server, "runtime")
        # per-request presented token (never stored, never logged)
        auth = self.headers.get("Authorization", "")
        token: Optional[str] = None
        if auth.startswith("Bearer "):
            token = auth[len("Bearer "):].strip() or None
        if path == "/health/live":
            if not _check_bearer(runtime, token):
                _audit_auth_failure(runtime, "health/live")
                self._error(401, "unauthorized")
                return
            self._send_json(200, {"status": "ok"})
        elif path == "/health/ready":
            if not _check_bearer(runtime, token):
                _audit_auth_failure(runtime, "health/ready")
                self._error(401, "unauthorized")
                return
            self._send_json(200, {"status": "ready"})
        else:
            self._error(404, "unknown endpoint")

    # ---- POST /infer ---------------------------------------------------

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path not in ("/infer", "/stream"):
            self._error(404, "unknown endpoint")
            return
        runtime = getattr(self.server, "runtime")
        # per-request presented token (never stored, never logged)
        auth = self.headers.get("Authorization", "")
        token: Optional[str] = None
        if auth.startswith("Bearer "):
            token = auth[len("Bearer "):].strip() or None
        if not _check_bearer(runtime, token):
            _audit_auth_failure(runtime, "infer")
            self._error(401, "unauthorized")
            return
        length = self.headers.get("Content-Length")
        if length is None:
            self._error(400, "missing Content-Length")
            return
        try:
            body = self.rfile.read(int(length))
        except OSError:
            self._error(400, "read failed")
            return
        try:
            request = json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._error(400, "invalid JSON")
            return
        if not isinstance(request, dict):
            self._error(400, "request body must be a JSON object")
            return

        runtime = getattr(self.server, "runtime")
        # M3 single-flight: one inference at a time per worker.
        with runtime.infer_lock:
            context = dict(runtime.context)
            context["request_id"] = runtime.request_id()
            try:
                if path == "/stream":
                    stream_fn = getattr(runtime.adapter, "stream", None)
                    if stream_fn is None:
                        self._error(400, "streaming not supported by app")
                        return
                    self.send_response(200)
                    self.send_header("Content-Type", "application/x-ndjson")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    with adapter_network_guard(context):
                        for chunk in stream_fn(request, context):
                            self._send_stream_frame({"type": "chunk", "data": chunk})
                    self._send_stream_frame({"type": "completed"})
                    self.close_connection = True
                    return
                with adapter_network_guard(context):
                    result = runtime.adapter.infer(request, context)
            except Exception:
                log.exception("adapter.%s failed", "stream" if path == "/stream" else "infer")
                if path == "/stream":
                    try:
                        self._send_stream_frame({
                            "type": "error",
                            "error": {"code": "500", "message": "internal error"},
                        })
                    except Exception:
                        pass
                else:
                    self._error(500, "internal error")
                return
        self._send_json(200, {"result": result})


class WorkerRuntimeServer(ThreadingHTTPServer):
    """Threaded loopback server carrying the loaded worker state."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr: Any, runtime: Any) -> None:
        self.runtime = runtime
        super().__init__(addr, WorkerHandler)


def run_worker(
    app_root: str,
    port: int,
    data_dir: Optional[str] = None,
    credential_file: Optional[str] = None,
) -> None:
    """Initialize (load + adapter.initialize) then serve on 127.0.0.1.

    Any initialization failure raises -> main() exits non-zero -> the
    systemd unit fails -> crash-loop backoff at the supervisor.
    """
    manifest_path = os.path.join(app_root, "manifest.json")
    if not os.path.isfile(manifest_path):
        raise WorkerRuntimeError(f"manifest not found: {manifest_path}")
    with open(manifest_path, "r", encoding="utf-8") as fh:
        manifest = json.load(fh)

    # A1: models load INSIDE this worker process (E5 re-verified here).
    models = loader.load_artifacts(manifest, app_root)

    context = build_context(manifest, models, data_dir)
    with adapter_network_guard(context):
        adapter = load_adapter(manifest, app_root)

    if data_dir:
        os.makedirs(data_dir, exist_ok=True)

    # Day 2 (P3): the credential is loaded BEFORE serving; the worker
    # fails closed when none is configured (see load_worker_secret).
    worker_secret = load_worker_secret(credential_file)

    with adapter_network_guard(context):
        adapter.initialize(context)  # R4: failure -> not ready -> unit fails

    runtime = _SimpleNamespace(
        adapter=adapter,
        context=context,
        infer_lock=threading.Lock(),
        request_id=lambda: "w-" + os.urandom(6).hex(),
        secret=worker_secret,
        app_id=manifest.get("app_id", "?"),
        data_dir=data_dir,
    )
    httpd = WorkerRuntimeServer((WORKER_BIND_HOST, port), runtime)

    # P4 / R4 bounded shutdown: SIGTERM -> graceful adapter.shutdown(),
    # then the serve loop exits; systemd TimeoutStopSec (30 s default)
    # escalates to SIGKILL when the process ignores SIGTERM.
    import signal

    def _on_sigterm(signum: int, frame: Any) -> None:  # noqa: ARG001
        log.info("SIGTERM received: graceful shutdown")
        try:
            adapter.shutdown()
        except Exception:
            log.exception("adapter.shutdown failed")
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _on_sigterm)

    log.info("worker %s ready on %s:%s", manifest.get("app_id", "?"), WORKER_BIND_HOST, port)
    httpd.serve_forever()


class _SimpleNamespace:
    """Tiny attribute namespace (keeps the server object decoupled)."""

    def __init__(self, **kw: Any) -> None:
        self.__dict__.update(kw)


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(prog="sydeco-lightml-worker")
    ap.add_argument("--app-root", required=True, help="versioned app dir (manifest.json)")
    ap.add_argument("--port", type=int, required=True, help="Core-assigned loopback port")
    ap.add_argument("--data-dir", default=None, help="app's own writable data dir")
    ap.add_argument(
        "--credential-file", default=None,
        help="dev/test credential file (path only; the secret is never on "
        "the command line). Production uses systemd LoadCredential=.",
    )
    args = ap.parse_args(argv)
    try:
        run_worker(
            args.app_root, args.port, args.data_dir, args.credential_file
        )
    except WorkerRuntimeError as exc:
        log.error("worker init failed: %s", exc)
        return 1
    except KeyboardInterrupt:
        return 0
    except Exception:
        log.exception("worker fatal error")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
