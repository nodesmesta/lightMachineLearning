"""Phase 2 / Day 1B — timeout containment (reviewer closure item #1, 2026-08-26).

Day 1 (24-08) proved: inference_timeout -> HTTP 504 at the Core edge. Day 1B
proves the REST of the reviewer's preferred behaviour (verbatim): "Inference
timeout -> return 504 -> terminate/recycle that capability worker -> restart
cleanly -> readiness false until the replacement worker is ready", plus
"another application continues working during the whole sequence".

Both hosts are covered:
- SystemdTransientWorkerHost (production path): subprocess.run is
  monkeypatched (24-08 pattern — worker.py imports subprocess inside
  methods) while a REAL loopback HTTP server stands in for the worker;
- InProcessWorkerHost (dev host) through the full CoreService + HTTP
  surface (tests/_http_harness.py).

The stalled Adapter is payload-triggered (decision B1, locked 2026-08-26):
it blocks ONLY on {"text": "STALL"} and returns instantly for any normal
input, so the REPLACEMENT worker can be proven to serve a normal inference
with 200. Readiness during the recycle window is BACKOFF (decision A1) ->
subsequent requests get an immediate 503 until the replacement is READY.
"""
from __future__ import annotations

import hashlib
import json
import os
import pickle
import shutil
import tempfile
import threading
import time
import types
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from sydeco_lightml_core.audit import JsonlAuditBackend
from sydeco_lightml_core.adapter import Adapter
from sydeco_lightml_core.core import CoreService
from sydeco_lightml_core.health import ReadinessStore
from sydeco_lightml_core.worker import (
    InferenceTimeout,
    InProcessWorkerHost,
    SystemdTransientWorkerHost,
    WorkerNotReady,
)

from tests._http_harness import HttpHarness
from tests._signing import sign_manifest

# ---------------------------------------------------------------------------
# shared pieces
# ---------------------------------------------------------------------------


class _StallAdapter(Adapter):
    """Payload-triggered stall (B1): blocks ONLY on {"text": "STALL"}.

    ``init_sleep`` makes the replacement worker's startup observable as a
    not-ready window; ``stall_sleep`` is the blocked-inference duration
    (shorter than a full test run so the abandoned thread cannot delay the
    interpreter exit for long).
    """

    def __init__(self, init_sleep: float = 0.8, stall_sleep: float = 2.0) -> None:
        self._init_sleep = init_sleep
        self._stall_sleep = stall_sleep

    def initialize(self, context) -> None:
        time.sleep(self._init_sleep)

    def infer(self, request, context):
        if isinstance(request, dict) and request.get("text") == "STALL":
            time.sleep(self._stall_sleep)
        return {"label": "a-ok"}

    def shutdown(self) -> None:
        pass


STALL_ADAPTER_SRC = """\
import time


class Adapter:
    def initialize(self, context):
        time.sleep(0.8)

    def infer(self, request, context):
        if isinstance(request, dict) and request.get("text") == "STALL":
            time.sleep(2.0)
        return {"label": "a-ok"}
"""

OK_ADAPTER_SRC = """\
class Adapter:
    def initialize(self, context):
        pass

    def infer(self, request, context):
        return {"label": "b-ok"}
"""


class _FakeWorkerServer:
    """Real loopback HTTP server standing in for the systemd worker.

    The FIRST POST /infer stalls (models the stuck adapter in the old
    worker); every later request returns instantly (models the fresh
    replacement worker serving normally).
    """

    def __init__(self) -> None:
        self.httpd: ThreadingHTTPServer | None = None
        self.port = 0
        self._lock = threading.Lock()
        self._infer_count = 0

    def _handler_factory(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format, *args):  # noqa: A002
                pass

            def send_error(self, code, message=None, explain=None):
                # http.server's own error path (e.g. parse_request after the
                # Core client timed out and closed) also writes to the dead
                # socket — silence it like _send
                try:
                    super().send_error(code, message, explain)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass

            def _send(self, code, obj):
                try:
                    body = json.dumps(obj).encode("utf-8")
                    self.send_response(code)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    # the Core client already timed out (504) and closed the
                    # connection — expected for the stalled first inference
                    pass

            def do_GET(self):  # noqa: N802
                self._send(200, {"status": "ready"})

            def do_POST(self):  # noqa: N802
                with server._lock:
                    server._infer_count += 1
                    n = server._infer_count
                if n == 1:
                    # the stalled adapter: the FIRST inference never returns
                    # within the client's inference_timeout
                    time.sleep(2.0)
                self._send(200, {"result": "ok"})

        return Handler

    def start(self) -> None:
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), self._handler_factory())
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self) -> None:
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()


class _FakeSubprocess:
    """Fake subprocess.run capturing argv; systemd-run "starts" are slow
    after the first one (observable not-ready window during recycle)."""

    def __init__(self, restart_delay: float = 1.5) -> None:
        self.calls = []
        self._lock = threading.Lock()
        self.restart_delay = restart_delay
        self._run_count = 0

    def run(self, argv, **kwargs):
        with self._lock:
            self.calls.append(list(argv))
        name = argv[0]
        if name == "systemd-run":
            with self._lock:
                self._run_count += 1
                n = self._run_count
            if n >= 2:
                # simulated slow unit (re)start: the replacement worker takes
                # time to come up -> observable not-ready window
                time.sleep(self.restart_delay)
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        if name == "systemctl":
            if len(argv) > 1 and argv[1] == "show":
                if "--value" in argv:
                    return types.SimpleNamespace(
                        returncode=0, stdout="inactive\n", stderr=""
                    )
                return types.SimpleNamespace(
                    returncode=0,
                    stdout="ActiveState=active\nResult=\nNRestarts=0\n",
                    stderr="",
                )
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------


class TimeoutRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="sydeco-timeout-")

    def tearDown(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)

    @staticmethod
    def _read_audit(audit_dir: str) -> list:
        entries = []
        path = os.path.join(audit_dir, "audit.jsonl")
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        entries.append(json.loads(line))
        return entries

    def _install_app(self, core: CoreService, app_id: str, adapter_src: str) -> dict:
        app_dir = os.path.join(self._tmp, app_id)
        os.makedirs(os.path.join(app_dir, "models"), exist_ok=True)
        os.makedirs(os.path.join(app_dir, "adapter"), exist_ok=True)
        model_bytes = pickle.dumps({"m": 1})
        with open(os.path.join(app_dir, "models", "model.pkl"), "wb") as fh:
            fh.write(model_bytes)
        with open(os.path.join(app_dir, "adapter", "main.py"), "w", encoding="utf-8") as fh:
            fh.write(adapter_src)
        manifest = {
            "manifest_version": 1,
            "app_id": app_id,
            "name": app_id,
            "version": "1.0.0",
            "capabilities": ["inference"],
            "models": [
                {
                    "role": "model",
                    "file": "models/model.pkl",
                    "format": "pickle",
                    "sha256": hashlib.sha256(model_bytes).hexdigest(),
                }
            ],
            "adapter": {
                "entry": "adapter/main.py",
                "files": [
                    {
                        "file": "adapter/main.py",
                        "sha256": hashlib.sha256(
                            adapter_src.encode("utf-8")
                        ).hexdigest(),
                    }
                ],
            },
            "release": {"key_id": "sydeco-test-key-v1", "signature": "manifest.sig"},
            "input_schema": {
                "type": "object",
                "required": ["text"],
                "properties": {"text": {"type": "string"}},
            },
            "output_schema": {
                "type": "object",
                "required": ["label"],
                "properties": {"label": {"type": "string"}},
            },
            "permissions": {"network": "none"},
            "api": {"authentication": "token"},
            "resource_limits": {
                "max_memory": 1073741824,
                "max_cpu": 100,
                "inference_timeout": 0.4,
                "concurrency": 1,
            },
            "dependencies": [],
        }
        manifest_path = os.path.join(app_dir, "manifest.json")
        with open(manifest_path, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh)
        sign_manifest(app_dir)
        ok, message, entry = core.install_app(manifest_path)
        self.assertTrue(ok, f"install failed: {message!r}")
        self.assertIsNotNone(entry)
        assert entry is not None
        return entry

    # -- test 1: production path (SystemdTransientWorkerHost) --------------

    def test_01_systemd_host_recycles_after_timeout(self) -> None:
        import subprocess as _subprocess

        server = _FakeWorkerServer()
        server.start()
        audit = JsonlAuditBackend(os.path.join(self._tmp, "audit1"))
        readiness = ReadinessStore()
        host = SystemdTransientWorkerHost(
            readiness=readiness, audit=audit,
            poll_interval=0.1, ready_wait=5.0,
        )
        context = {
            "app_root": self._tmp,
            "config": {"resource_limits": {"inference_timeout": 0.3}},
            "data_dir": os.path.join(self._tmp, "data1"),
            "port": server.port,
            "user": "",
            "cwd": "",
            "pythonpath": "",
        }
        fake = _FakeSubprocess(restart_delay=1.5)
        orig_run = _subprocess.run
        _subprocess.run = fake.run
        # cleanup order (LIFO): stop monitor, stop server, restore subprocess
        self.addCleanup(host.shutdown)
        self.addCleanup(server.stop)
        self.addCleanup(self._restore_subprocess, _subprocess, orig_run)

        try:
            host.start("app-x", "1.0.0", None, context)
            self.assertTrue(host._ready)

            # 1) stalled inference -> timeout exceeded -> 504
            with self.assertRaises(InferenceTimeout):
                host.infer({"text": "STALL"}, "r1")

            # 2) readiness false until the replacement worker is ready
            #    (BACKOFF set synchronously; stop() may briefly flip IDLE)
            self.assertNotEqual(readiness.status("app-x"), "ready")
            with self.assertRaises(WorkerNotReady):
                host.infer({"text": "x"}, "r-busy")

            # 3) the background recycle relaunches the unit (2nd systemd-run)
            #    and the readiness flips back to ready
            deadline = time.time() + 6
            while time.time() < deadline and not readiness.is_ready("app-x"):
                time.sleep(0.05)
            self.assertTrue(
                readiness.is_ready("app-x"),
                f"worker did not become ready again: {readiness.status('app-x')}",
            )

            # 4) next normal inference returns 200-equivalent (result)
            self.assertEqual(host.infer({"text": "hello"}, "r2"), "ok")

            run_calls = [c for c in fake.calls if c[0] == "systemd-run"]
            self.assertGreaterEqual(len(run_calls), 2)  # initial + recycle
            stop_calls = [
                c for c in fake.calls
                if c[0] == "systemctl" and len(c) > 1 and c[1] == "stop"
            ]
            self.assertTrue(stop_calls, "worker must be terminated (bounded stop)")

            entries = self._read_audit(os.path.join(self._tmp, "audit1"))
            actions = [e.get("action") for e in entries]
            self.assertIn("INFERENCE_TIMEOUT", actions)
            self.assertTrue(
                any(
                    e.get("action") == "WORKER_RESTART"
                    and e.get("detail") == "timeout recycle"
                    for e in entries
                ),
                "WORKER_RESTART (timeout recycle) must be audited",
            )
        finally:
            _subprocess.run = orig_run

    @staticmethod
    def _restore_subprocess(mod, orig) -> None:
        mod.run = orig

    # -- test 2: continuity — another app keeps working (full CoreService) --

    def test_02_second_app_keeps_serving_during_recycle(self) -> None:
        core = CoreService(data_dir=os.path.join(self._tmp, "data2"))
        stall_entry = self._install_app(core, "stall-app", STALL_ADAPTER_SRC)
        ok_entry = self._install_app(core, "ok-app", OK_ADAPTER_SRC)
        ok_start, msg, _ = core.start_app("stall-app")
        self.assertTrue(ok_start, msg)
        ok_start2, msg2, _ = core.start_app("ok-app")
        self.assertTrue(ok_start2, msg2)

        harness = HttpHarness(core)
        harness.start()
        self.addCleanup(harness.stop)

        stall_token = stall_entry["token"]
        ok_token = ok_entry["token"]

        # 1) stalled inference -> timeout exceeded -> 504 returned
        status, body = harness.infer("stall-app", {"text": "STALL"}, stall_token)
        self.assertEqual(status, 504)
        self.assertIn("inference timeout", body["error"]["message"])

        # 2) readiness false (BACKOFF) until the replacement worker is ready
        self.assertNotEqual(core.readiness.status("stall-app"), "ready")

        # 3) ANOTHER application keeps working during the whole sequence
        status2, body2 = harness.infer("ok-app", {"text": "x"}, ok_token)
        self.assertEqual(status2, 200)
        self.assertEqual(body2["result"]["label"], "b-ok")

        # 4) during the window the recycled app is gated -> immediate 503
        status3, _ = harness.infer("stall-app", {"text": "x"}, stall_token)
        self.assertEqual(status3, 503)

        # 5) readiness restored -> next normal inference returns 200
        deadline = time.time() + 6
        while time.time() < deadline and not core.readiness.is_ready("stall-app"):
            time.sleep(0.05)
        self.assertTrue(
            core.readiness.is_ready("stall-app"),
            f"stall-app did not become ready again: "
            f"{core.readiness.status('stall-app')}",
        )
        status4, body4 = harness.infer("stall-app", {"text": "hello"}, stall_token)
        self.assertEqual(status4, 200)
        self.assertEqual(body4["result"]["label"], "a-ok")

        entries = self._read_audit(os.path.join(self._tmp, "data2", "audit"))
        actions = [e.get("action") for e in entries]
        self.assertIn("INFERENCE_TIMEOUT", actions)
        self.assertTrue(
            any(
                e.get("action") == "WORKER_RESTART"
                and e.get("app_id") == "stall-app"
                and e.get("detail") == "timeout recycle"
                for e in entries
            ),
            "WORKER_RESTART (timeout recycle) must be audited for stall-app",
        )

    # -- test 3: dev host (InProcessWorkerHost) direct ---------------------

    def test_03_inprocess_host_recycles_after_timeout(self) -> None:
        audit = JsonlAuditBackend(os.path.join(self._tmp, "audit3"))
        readiness = ReadinessStore()
        host = InProcessWorkerHost(readiness=readiness, audit=audit)
        self.addCleanup(host.shutdown)
        host.start(
            "app-x",
            "1.0.0",
            _StallAdapter(init_sleep=0.8, stall_sleep=2.0),
            {
                "config": {"resource_limits": {"inference_timeout": 0.4}},
                "data_dir": os.path.join(self._tmp, "data3"),
            },
        )
        self.assertEqual(host.status("app-x"), "ready")

        with self.assertRaises(InferenceTimeout):
            host.infer({"text": "STALL"}, "r1")

        # readiness false (BACKOFF, synchronous) until the replacement ready
        # (the readiness STORE is the authoritative gate; host.status() may
        # flip early because restart() clears _broken at its start)
        self.assertEqual(readiness.status("app-x"), "backoff")
        with self.assertRaises(WorkerNotReady):
            host.infer({"text": "x"}, "r-busy")

        deadline = time.time() + 6
        while time.time() < deadline and not readiness.is_ready("app-x"):
            time.sleep(0.05)
        self.assertTrue(readiness.is_ready("app-x"))

        result = host.infer({"text": "hello"}, "r2")
        self.assertEqual(result, {"label": "a-ok"})

        entries = self._read_audit(os.path.join(self._tmp, "audit3"))
        actions = [e.get("action") for e in entries]
        self.assertIn("INFERENCE_TIMEOUT", actions)
        self.assertTrue(
            any(
                e.get("action") == "WORKER_RESTART"
                and e.get("detail") == "timeout recycle"
                for e in entries
            ),
            "WORKER_RESTART (timeout recycle) must be audited",
        )


if __name__ == "__main__":
    unittest.main()
