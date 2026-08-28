"""Phase 2 / Day 2 — authenticated Core <-> worker channel (2026-08-28).

Fase A tests (P1..P3 of this day's task.md; reviewer order P0..P2):

- P1 (reviewer P0): two SIMULTANEOUS timeout conditions on the same
  capability must produce EXACTLY ONE worker recycle, one active
  replacement generation and one timeout-driven WORKER_RESTART. The
  SystemdTransientWorkerHost._recycling check/set is protected by a small
  local lock (the Core HTTP server is multithreaded — reviewer finding).
- P2 (reviewer P1): a cryptographically random secret per worker
  generation — unique per app, unique per generation, rotated on every
  recycle/restart (both hosts), never reused across applications.
- P3 (reviewer P2): secure credential delivery — systemd LoadCredential=
  via the transient-unit property mechanism (systemd 249 has no
  --load-credential CLI option), backed by an ephemeral root-only (0600)
  file unique per launch, removed on stop; the worker reads
  $CREDENTIALS_DIRECTORY/worker-secret (production) or --credential-file
  (dev/test; path only, the secret never appears in argv/env/manifest/
  registry/audit/logs/errors/bundle). The worker FAILS CLOSED when no
  credential is configured.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from sydeco_lightml_core.audit import JsonlAuditBackend
from sydeco_lightml_core.core import CoreService
from sydeco_lightml_core.health import ReadinessStore
from sydeco_lightml_core.worker import (
    InferenceTimeout,
    InProcessWorkerHost,
    SystemdTransientWorkerHost,
    WorkerNotReady,
)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _StallingWorkerServer:
    """Real loopback HTTP server standing in for the systemd worker.

    The first ``stall_count`` POST /infer requests stall (models stuck
    adapters in old worker generations); later requests return instantly.
    """

    def __init__(self, stall_count: int = 1) -> None:
        self.httpd: ThreadingHTTPServer | None = None
        self.port = 0
        self._lock = threading.Lock()
        self._infer_count = 0
        self._stall_count = stall_count

    def _handler_factory(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format, *args):  # noqa: A002
                pass

            def send_error(self, code, message=None, explain=None):
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
                    pass

            def do_GET(self):  # noqa: N802
                self._send(200, {"status": "ready"})

            def do_POST(self):  # noqa: N802
                with server._lock:
                    server._infer_count += 1
                    n = server._infer_count
                if n <= server._stall_count:
                    # the stuck adapter generation(s): never return within
                    # the client's inference_timeout
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
    """Fake subprocess.run capturing argv; systemd-run relaunches are slow
    (observable not-ready window during recycle)."""

    def __init__(self, restart_delay: float = 1.0) -> None:
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


class ChannelAuthFaseATests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="sydeco-autha-")

    def tearDown(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _restore_subprocess(self, mod, orig) -> None:
        mod.run = orig

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

    # ---- P1: production recycle concurrency ---------------------------

    def test_01_concurrent_timeouts_single_recycle(self) -> None:
        """Two simultaneous timeouts on the SAME capability -> exactly one
        recycle, one replacement generation, one WORKER_RESTART (reviewer
        P0)."""
        import subprocess as _subprocess

        server = _StallingWorkerServer(stall_count=2)  # BOTH requests stall
        server.start()
        audit = JsonlAuditBackend(os.path.join(self._tmp, "audit"))
        readiness = ReadinessStore()
        host = SystemdTransientWorkerHost(
            readiness=readiness, audit=audit,
            poll_interval=0.05, ready_wait=5.0,
        )
        context = {
            "app_root": self._tmp,
            "config": {"resource_limits": {"inference_timeout": 0.3}},
            "data_dir": os.path.join(self._tmp, "data"),
            "port": server.port,
            "user": "",
            "cwd": "",
            "pythonpath": "",
        }
        fake = _FakeSubprocess(restart_delay=1.0)
        orig_run = _subprocess.run
        _subprocess.run = fake.run
        self.addCleanup(host.shutdown)
        self.addCleanup(server.stop)
        self.addCleanup(self._restore_subprocess, _subprocess, orig_run)

        host.start("app-a", "1.0.0", None, context)
        self.assertTrue(host._ready)

        barrier = threading.Barrier(2)
        results = []
        errors = []

        def _attack() -> None:
            barrier.wait()
            try:
                host.infer({"text": "STALL"}, "r1")
                results.append("ok")
            except InferenceTimeout:
                results.append("timeout")
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=_attack) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        self.assertEqual(errors, [], f"unexpected non-timeout errors: {errors}")
        self.assertEqual(results, ["timeout", "timeout"],
                         "both simultaneous requests must time out")

        # readiness recovers to READY (the single replacement generation) —
        # waited on FIRST so the recycle thread has fully completed
        deadline = time.time() + 6
        while time.time() < deadline and not readiness.is_ready("app-a"):
            time.sleep(0.05)
        self.assertTrue(
            readiness.is_ready("app-a"),
            f"replacement did not become ready: {readiness.status('app-a')}",
        )

        # exactly one recycle: initial launch + ONE relaunch
        run_calls = [c for c in fake.calls if c[0] == "systemd-run"]
        self.assertEqual(
            len(run_calls), 2,
            f"expected exactly ONE recycle, got systemd-run calls: "
            f"{[c[1] for c in run_calls]}",
        )

        # exactly one timeout-driven WORKER_RESTART audit
        entries = self._read_audit(os.path.join(self._tmp, "audit"))
        restarts = [
            e for e in entries
            if e.get("action") == "WORKER_RESTART"
            and e.get("detail") == "timeout recycle"
        ]
        self.assertEqual(len(restarts), 1, f"WORKER_RESTART entries: {restarts}")

        # the surviving generation serves normally
        self.assertEqual(host.infer({"text": "hello"}, "r2"), "ok")

    # ---- P2: per-generation secret ------------------------------------

    def test_02_systemd_host_secret_per_generation(self) -> None:
        import subprocess as _subprocess

        audit = JsonlAuditBackend(os.path.join(self._tmp, "audit"))
        readiness = ReadinessStore()
        server = _StallingWorkerServer(stall_count=0)
        server.start()
        host = SystemdTransientWorkerHost(
            readiness=readiness, audit=audit,
            poll_interval=0.05, ready_wait=5.0,
        )
        cred_dir = os.path.join(self._tmp, "secrets")
        context = {
            "app_root": self._tmp,
            "config": {"resource_limits": {"inference_timeout": 2.0}},
            "data_dir": os.path.join(self._tmp, "data"),
            "port": server.port,
            "user": "",
            "cwd": "",
            "pythonpath": "",
            "credential_dir": cred_dir,
        }
        fake = _FakeSubprocess(restart_delay=0.2)
        orig_run = _subprocess.run
        _subprocess.run = fake.run
        self.addCleanup(host.shutdown)
        self.addCleanup(server.stop)
        self.addCleanup(self._restore_subprocess, _subprocess, orig_run)

        host.start("app-a", "1.0.0", None, context)
        secret_a1 = host._secret
        self.assertIsNotNone(secret_a1)
        assert secret_a1 is not None
        self.assertEqual(len(secret_a1), 64, "token_hex(32) -> 64 hex chars")
        self.assertNotEqual(secret_a1, "a" * 64)

        # generation 2 (recycle via restart) -> fresh secret
        host.restart("app-a")
        secret_a2 = host._secret
        self.assertIsNotNone(secret_a2)
        assert secret_a2 is not None
        self.assertNotEqual(secret_a1, secret_a2, "secret must rotate per recycle")

        # a SECOND app gets its OWN secret (never shared across apps)
        host2 = SystemdTransientWorkerHost(
            readiness=readiness, audit=audit,
            poll_interval=0.05, ready_wait=5.0,
        )
        context2 = dict(context)
        context2["port"] = server.port
        fake2 = _FakeSubprocess(restart_delay=0.2)
        self.addCleanup(host2.shutdown)
        self.addCleanup(self._restore_subprocess, _subprocess, orig_run)
        _subprocess.run = fake2.run
        host2.start("app-b", "1.0.0", None, context2)
        secret_b = host2._secret
        self.assertIsNotNone(secret_b)
        assert secret_b is not None
        self.assertNotEqual(secret_a1, secret_b)
        self.assertNotEqual(secret_a2, secret_b)

    def test_03_inprocess_host_secret_rotation(self) -> None:
        from sydeco_lightml_core.adapter import Adapter

        class _OkAdapter(Adapter):
            def initialize(self, context) -> None:
                pass

            def infer(self, request, context):
                return {"label": "ok"}

            def shutdown(self) -> None:
                pass

        host = InProcessWorkerHost(readiness=ReadinessStore())
        host.start("app-x", "1.0.0", _OkAdapter(), {"config": {}})
        s1 = host._secret
        self.assertEqual(len(s1), 64)
        host.restart("app-x")
        s2 = host._secret
        self.assertNotEqual(s1, s2, "restart must rotate the in-memory secret")

    # ---- P3: secure credential delivery -------------------------------

    def test_04_systemd_host_loadcredential_property_and_file(self) -> None:
        import subprocess as _subprocess

        audit = JsonlAuditBackend(os.path.join(self._tmp, "audit"))
        readiness = ReadinessStore()
        server = _StallingWorkerServer(stall_count=0)
        server.start()
        host = SystemdTransientWorkerHost(
            readiness=readiness, audit=audit,
            poll_interval=0.05, ready_wait=5.0,
        )
        cred_dir = os.path.join(self._tmp, "secrets")
        context = {
            "app_root": self._tmp,
            "config": {"resource_limits": {"inference_timeout": 2.0}},
            "data_dir": os.path.join(self._tmp, "data"),
            "port": server.port,
            "user": "",
            "cwd": "",
            "pythonpath": "",
            "credential_dir": cred_dir,
        }
        fake = _FakeSubprocess(restart_delay=0.2)
        orig_run = _subprocess.run
        _subprocess.run = fake.run
        self.addCleanup(host.shutdown)
        self.addCleanup(server.stop)
        self.addCleanup(self._restore_subprocess, _subprocess, orig_run)

        host.start("app-a", "1.0.0", None, context)

        # LoadCredential= via the transient-unit property mechanism
        run_calls = [c for c in fake.calls if c[0] == "systemd-run"]
        self.assertEqual(len(run_calls), 1)
        argv = " ".join(run_calls[0])
        self.assertIn("LoadCredential=worker-secret:", argv)
        cred_path = host._credential_path
        self.assertIsNotNone(cred_path)
        assert cred_path is not None
        self.assertIn(f"LoadCredential=worker-secret:{cred_path}", argv)

        # the source file is ephemeral, root-only (0600), unique per launch,
        # holds the CURRENT generation secret
        self.assertTrue(os.path.isfile(cred_path))
        self.assertEqual(os.stat(cred_path).st_mode & 0o777, 0o600)
        with open(cred_path, "r", encoding="utf-8") as fh:
            self.assertEqual(fh.read().strip(), host._secret)

        # NEVER on the command line / Environment
        self.assertIsNotNone(host._secret)
        assert host._secret is not None
        self.assertNotIn(host._secret, argv)
        joined_env = " ".join(
            str(a) for a in run_calls[0] if str(a).startswith("Environment=")
        )
        self.assertNotIn(host._secret, joined_env)

        # recycle -> fresh file, fresh secret; the new secret is NEVER in
        # any systemd-run argv (old or new)
        old_path = cred_path
        host.restart("app-a")
        new_path = host._credential_path
        self.assertIsNotNone(new_path)
        assert new_path is not None
        self.assertNotEqual(old_path, new_path, "credential file per launch")
        self.assertTrue(os.path.isfile(new_path))
        for call in fake.calls:
            if call[0] == "systemd-run":
                self.assertNotIn(host._secret, " ".join(call))

        host.stop("app-a")
        self.assertFalse(
            os.path.exists(new_path), "credential file must be removed on stop"
        )

    def _spawn_worker(self, app_root: str, port: int,
                      cred_file: str | None,
                      cred_dir_env: str | None = None):
        cmd = [sys.executable, "-m", "sydeco_lightml_core.worker_runtime",
               "--app-root", app_root, "--port", str(port)]
        if cred_file:
            cmd += ["--credential-file", cred_file]
        env = dict(
            os.environ,
            PYTHONPYCACHEPREFIX=tempfile.mkdtemp(prefix="sydeco-autha-pycache-"),
        )
        if cred_dir_env:
            env["CREDENTIALS_DIRECTORY"] = cred_dir_env
        return subprocess.Popen(
            cmd, cwd=REPO, env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    def _probe(self, port: int, path: str, token: str | None,
               timeout: float = 1.0) -> int:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        headers = {}
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        conn.request("GET", path, headers=headers)
        resp = conn.getresponse()
        resp.read()
        conn.close()
        return resp.status

    def test_05_worker_runtime_serves_with_credential(self) -> None:
        import socket

        app_root = os.path.join(REPO, "examples", "text-classifier")
        cred_file = os.path.join(self._tmp, "worker-secret")
        with open(cred_file, "w", encoding="utf-8") as fh:
            fh.write("d" * 64)
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        proc = self._spawn_worker(app_root, port, cred_file)
        try:
            deadline = time.time() + 20
            ready = False
            while time.time() < deadline:
                if proc.poll() is not None:
                    break
                try:
                    if self._probe(port, "/health/ready", "d" * 64) == 200:
                        ready = True
                        break
                except Exception:
                    pass
                time.sleep(0.2)
            self.assertTrue(ready, "worker with credential did not become ready")
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()

    def test_06_worker_runtime_reads_credentials_directory(self) -> None:
        import socket

        app_root = os.path.join(REPO, "examples", "text-classifier")
        cred_dir = os.path.join(self._tmp, "cred-dir")
        os.makedirs(cred_dir, exist_ok=True)
        with open(os.path.join(cred_dir, "worker-secret"), "w",
                  encoding="utf-8") as fh:
            fh.write("e" * 64)
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        proc = self._spawn_worker(app_root, port, None, cred_dir_env=cred_dir)
        try:
            deadline = time.time() + 20
            ready = False
            while time.time() < deadline:
                if proc.poll() is not None:
                    break
                try:
                    if self._probe(port, "/health/ready", "e" * 64) == 200:
                        ready = True
                        break
                except Exception:
                    pass
                time.sleep(0.2)
            self.assertTrue(
                ready, "worker with CREDENTIALS_DIRECTORY did not become ready"
            )
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()

    def test_07_worker_runtime_fails_closed_without_credential(self) -> None:
        import socket

        app_root = os.path.join(REPO, "examples", "text-classifier")
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        proc = self._spawn_worker(app_root, port, None)
        try:
            rc = proc.wait(timeout=20)
            self.assertNotEqual(
                rc, 0,
                "worker without a credential must fail closed (non-zero exit)",
            )
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
