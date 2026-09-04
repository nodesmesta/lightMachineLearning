"""HTTP serving surface (V2.1 proposal 5.1 / 5.2 + 2.5/B1 K1-K7, 2.6/B2 H3,
2.11/C3 M3/M4, 3.0/R1) — stdlib http.server (user decision D3).

Single entry point on 127.0.0.1 (A3). Endpoints (5.1):
  GET  /health, /health/live, /health/ready (per-app aggregate, G2 —
       NOT a global gate), /health/apps (G4)
  GET  /api/v1/apps
  POST /api/v1/apps/{app_id}/infer   (SINGLE + BATCH, K5; STREAMING is
       OUT OF SCOPE Day 2, mirroring 7.1 LIMITS)

Edge responsibilities: auth token (K2/R8 -> 401), body-size limit (K5 ->
400), JSON-object requirement, input_schema validation (H1/H3 -> 400)
BEFORE the request reaches the worker; output_schema validation (H2 ->
500 sanitized) before the B1 envelope; readiness gate (G2/G3 -> 503);
inference timeout (M4 -> 504). Errors are ALWAYS sanitized (P4); full
detail goes to the server log + audit only.
"""
from __future__ import annotations

import json
import logging
import re
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from .health import AppStatus
from .schema import validate_schema
from .secrets import verify_token
from .worker import (
    InferenceTimeout,
    StreamBackpressure,
    StreamRestarted,
    StreamTimeout,
    StreamUnsupported,
    WorkerNotReady,
)

log = logging.getLogger("sydeco-lightml.server")

DEFAULT_BODY_LIMIT = 1 * 1024 * 1024  # K5 default (1 MiB)
READY_WAIT_SECONDS = 30.0  # G3 bounded wait during start/restart

_INFER_RE = re.compile(r"^/api/v1/apps/(?P<app_id>[a-z0-9-]+)/infer$")


def new_request_id() -> str:
    return uuid.uuid4().hex[:12]


class CoreHTTPServer(ThreadingHTTPServer):
    """Threaded loopback server carrying the CoreService instance."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        addr: Tuple[str, int],
        service: Any,
        body_limit: int = DEFAULT_BODY_LIMIT,
    ) -> None:
        self.service = service
        self.body_limit = body_limit
        super().__init__(addr, CoreHandler)


class CoreHandler(BaseHTTPRequestHandler):
    server_version = "sydeco-lightml/2.0-dev"
    protocol_version = "HTTP/1.1"

    # ---- helpers -------------------------------------------------------

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        # request-line noise goes to the app logger (not stdout spam);
        # every request is recorded in the C2 audit with duration/status.
        log.debug("http: " + format % args)

    def _send_json(self, code: int, obj: Any) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_ndjson_event(self, event: Dict[str, Any]) -> None:
        """Write exactly one JSON object as one NDJSON line (K5)."""
        self.wfile.write(json.dumps(event).encode("utf-8") + b"\n")
        self.wfile.flush()

    def _read_body(self, limit: int) -> Optional[bytes]:
        """Read the request body, enforcing the K5 size limit.

        Returns None when the payload exceeds the limit (caller replies
        400 edge rejection, H3 — never reaches the worker).
        """
        length = self.headers.get("Content-Length")
        if length is None:
            return b""
        try:
            length = int(length)
        except ValueError:
            return None
        if length > limit:
            return None
        return self.rfile.read(length)

    def _auth_ok(self, app_id: str, manifest: Dict[str, Any]) -> bool:
        """K2/R8: per-app Bearer token, verified at the edge."""
        if manifest.get("api", {}).get("authentication", "token") == "none":
            return True  # R8a explicit opt-in for trusted loopback only
        header = self.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return False
        return verify_token(self.server.service.data_dir, app_id, header[7:].strip())

    def _ready_or_wait(self, app_id: str) -> Tuple[bool, str]:
        """G2/G3: readiness gate for the TARGET app only."""
        status = self.server.service.readiness.status(app_id)
        if status == AppStatus.READY.value:
            return True, ""
        if status == AppStatus.LOADING.value:
            deadline = time.time() + READY_WAIT_SECONDS
            while time.time() < deadline:
                time.sleep(0.1)
                status = self.server.service.readiness.status(app_id)
                if status == AppStatus.READY.value:
                    return True, ""
                if status != AppStatus.LOADING.value:
                    break
        return False, status

    def _audit(
        self, app_id: str, request_id: str, status: str, duration_ms: int,
        http_status: int, detail: str = "",
    ) -> None:
        try:
            self.server.service.audit.append(
                {
                    "action": "infer",
                    "app_id": app_id,
                    "result": status,
                    "request_id": request_id,
                    "duration_ms": duration_ms,
                    "http_status": http_status,
                    "detail": detail,
                }
            )
        except Exception:  # audit failure must never break serving (X5)
            log.exception("audit append failed (degraded)")

    # ---- GET -----------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        service = self.server.service
        try:
            if path == "/health":
                self._send_json(200, {
                    "status": "ok",
                    "apps": service.health_apps(),
                })
            elif path == "/health/live":
                self._send_json(200, {"status": "ok"})
            elif path == "/health/ready":
                # G2: per-app aggregate, NOT a global gate.
                self._send_json(200, {"apps": service.health_apps()})
            elif path == "/health/apps":
                self._send_json(200, service.health_apps())
            elif path == "/api/v1/apps":
                self._send_json(200, {"apps": service.api_apps()})
            else:
                self._send_json(404, {
                    "error": {"code": "404", "message": "unknown endpoint"},
                    "request_id": new_request_id(),
                })
        except Exception as exc:
            log.exception("GET %s failed", path)
            self._send_json(500, {
                "error": {"code": "500", "message": "internal error"},
                "request_id": new_request_id(),
            })

    # ---- POST /api/v1/apps/{app_id}/infer ------------------------------

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        match = _INFER_RE.match(path)
        if not match:
            self._send_json(404, {
                "error": {"code": "404", "message": "unknown endpoint"},
                "request_id": new_request_id(),
            })
            return
        self._handle_infer(match.group("app_id"))

    def _handle_infer(self, app_id: str) -> None:
        service = self.server.service
        request_id = new_request_id()
        started = time.time()

        info = service.router.resolve(app_id)
        if info is None:
            self._send_json(404, {
                "error": {"code": "404", "message": f"unknown app: {app_id}"},
                "request_id": request_id,
            })
            return
        manifest = info.get("manifest", {})
        version = info.get("active_version", "")

        # K2/R8 auth
        if not self._auth_ok(app_id, manifest):
            self._audit(app_id, request_id, "fail", 0, 401, "auth")
            self._send_json(401, {
                "error": {"code": "401", "message": "missing or invalid token"},
                "request_id": request_id,
            })
            return

        # K5/H3 edge: body size limit
        body = self._read_body(self.server.body_limit)
        if body is None:
            self._audit(app_id, request_id, "fail", 0, 400, "payload too large")
            self._send_json(400, {
                "error": {"code": "400", "message": "payload exceeds size limit"},
                "request_id": request_id,
            })
            return

        # body must be a JSON object (L1)
        try:
            payload = json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._audit(app_id, request_id, "fail", 0, 400, "invalid JSON")
            self._send_json(400, {
                "error": {"code": "400", "message": "invalid JSON body"},
                "request_id": request_id,
            })
            return
        if not isinstance(payload, dict):
            self._audit(app_id, request_id, "fail", 0, 400, "body not object")
            self._send_json(400, {
                "error": {"code": "400", "message": "request body must be a JSON object"},
                "request_id": request_id,
            })
            return

        # single vs batch (K5 / 5.2)
        input_schema = manifest.get("input_schema", {})
        if isinstance(payload.get("inputs"), list):
            items: List[Dict[str, Any]] = payload["inputs"]
            batch = True
        else:
            items = [payload]
            batch = False

        # H3 edge validation against input_schema — BEFORE the worker
        errors: List[str] = []
        for i, item in enumerate(items):
            if not isinstance(item, dict):
                errors.append(f"inputs[{i}]: must be an object")
                continue
            errs = validate_schema(item, input_schema, f"inputs[{i}]" if batch else "$")
            errors.extend(errs)
        if errors:
            self._audit(app_id, request_id, "fail", 0, 400, "schema violation")
            self._send_json(400, {
                "error": {
                    "code": "400",
                    "message": "invalid input: " + "; ".join(errors[:5]),
                },
                "request_id": request_id,
            })
            return

        # G2/G3 readiness gate (target app only)
        ok, status = self._ready_or_wait(app_id)
        if not ok:
            self._audit(app_id, request_id, "fail", 0, 503, f"not ready ({status})")
            self._send_json(503, {
                "error": {"code": "503", "message": f"app not ready: {app_id}"},
                "request_id": request_id,
            })
            return

        wants_stream = "application/x-ndjson" in self.headers.get("Accept", "")
        if wants_stream:
            if batch:
                self._audit(app_id, request_id, "fail", 0, 400, "streaming batch unsupported")
                self._send_json(400, {
                    "error": {
                        "code": "400",
                        "message": "streaming batch requests are not supported",
                    },
                    "request_id": request_id,
                })
                return
            self._handle_stream_infer(app_id, version, request_id, started, items[0])
            return

        # run (single-flight M3, timeout M4)
        host = service.worker_host(app_id)
        try:
            if batch:
                results: List[Any] = []
                for item in items:
                    results.append(host.infer(item, request_id))
                result_obj = {"results": results}
            else:
                result_obj = {"result": host.infer(items[0], request_id)}
        except InferenceTimeout:
            self._audit(app_id, request_id, "fail", 0, 504, "inference timeout")
            self._send_json(504, {
                "error": {"code": "504", "message": "inference timeout"},
                "request_id": request_id,
            })
            return
        except WorkerNotReady:
            self._audit(app_id, request_id, "fail", 0, 503, "worker not ready")
            self._send_json(503, {
                "error": {"code": "503", "message": f"app not ready: {app_id}"},
                "request_id": request_id,
            })
            return
        except Exception as exc:
            # H2/sanitized: no internal detail reaches the client
            log.exception("infer failed for %s", app_id)
            self._audit(app_id, request_id, "fail", 0, 500, f"infer exception: {type(exc).__name__}")
            self._send_json(500, {
                "error": {"code": "500", "message": "internal error"},
                "request_id": request_id,
            })
            return

        # H2: output validated against output_schema BEFORE the envelope
        output_schema = manifest.get("output_schema", {})
        if batch:
            out_errors: List[str] = []
            for i, r in enumerate(result_obj["results"]):
                out_errors.extend(validate_schema(r, output_schema, f"result[{i}]"))
        else:
            out_errors = validate_schema(result_obj["result"], output_schema, "result")
        if out_errors:
            log.warning("output schema violation for %s: %s", app_id, out_errors)
            self._audit(app_id, request_id, "fail", 0, 500, "output schema violation")
            self._send_json(500, {
                "error": {"code": "500", "message": "internal error"},
                "request_id": request_id,
            })
            return

        envelope = {
            "request_id": request_id,
            "app": app_id,
            "app_version": version,
        }
        envelope.update(result_obj)
        duration_ms = int((time.time() - started) * 1000)
        self._audit(app_id, request_id, "ok", duration_ms, 200)
        self._send_json(200, envelope)

    def _stream_event_base(self, app_id: str, version: str, request_id: str) -> Dict[str, Any]:
        return {"request_id": request_id, "app": app_id, "app_version": version}

    def _handle_stream_infer(
        self,
        app_id: str,
        version: str,
        request_id: str,
        started: float,
        item: Dict[str, Any],
    ) -> None:
        """K5 minimal NDJSON streaming path for POST /infer.

        Uses the same public endpoint, edge auth, validation and readiness path
        as normal inference. The adapter yields chunks; Core owns event
        metadata and JSONL framing so adapter newlines cannot alter protocol
        boundaries.
        """
        service = getattr(self.server, "service")
        host = service.worker_host(app_id)
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        base = self._stream_event_base(app_id, version, request_id)
        count = 0
        try:
            self._send_ndjson_event({**base, "event": "accepted"})
            for chunk in host.stream(item, request_id):
                self._send_ndjson_event({
                    **base,
                    "event": "chunk",
                    "seq": count,
                    "data": chunk,
                })
                count += 1
            self._send_ndjson_event({**base, "event": "completed", "chunks": count})
            duration_ms = int((time.time() - started) * 1000)
            self._audit(app_id, request_id, "ok", duration_ms, 200, "stream")
        except (BrokenPipeError, ConnectionResetError):
            duration_ms = int((time.time() - started) * 1000)
            self._audit(app_id, request_id, "cancelled", duration_ms, 499, "STREAM_CANCELLED")
        except InferenceTimeout as exc:
            reason = exc.reason if isinstance(exc, StreamTimeout) else "inference"
            self._audit(app_id, request_id, "fail", 0, 504, "stream timeout")
            self._send_ndjson_event({
                **base,
                "event": "timeout",
                "reason": reason,
                "code": "504",
                "message": "stream timeout",
            })
        except WorkerNotReady:
            self._audit(app_id, request_id, "fail", 0, 503, "worker not ready")
            self._send_ndjson_event({
                **base,
                "event": "worker_error",
                "code": "503",
                "message": "app not ready",
            })
        except StreamBackpressure:
            self._audit(app_id, request_id, "fail", 0, 429, "stream backpressure")
            self._send_ndjson_event({
                **base,
                "event": "worker_error",
                "code": "429",
                "message": "stream backpressure",
            })
        except StreamUnsupported:
            self._audit(app_id, request_id, "fail", 0, 400, "streaming not supported")
            self._send_ndjson_event({
                **base,
                "event": "worker_error",
                "code": "400",
                "message": "streaming not supported",
            })
        except StreamRestarted:
            duration_ms = int((time.time() - started) * 1000)
            self._audit(app_id, request_id, "fail", duration_ms, 503, "STREAM_RESTARTED")
            self._send_ndjson_event({
                **base,
                "event": "worker_error",
                "code": "503",
                "message": "worker restarted",
            })
        except Exception as exc:
            log.exception("stream infer failed for %s", app_id)
            self._audit(app_id, request_id, "fail", 0, 500, f"stream exception: {type(exc).__name__}")
            self._send_ndjson_event({
                **base,
                "event": "worker_error",
                "code": "500",
                "message": "internal error",
            })
