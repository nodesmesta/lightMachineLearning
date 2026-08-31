"""Phase 2 / Day 2 — P7 acceptance matrix (reviewer P6), non-privileged.

Maps the reviewer's 13-row acceptance table 1:1 to executable tests on
the dev equivalents (honest labels, D5 #5):

| Test | Expected | covered by |
|---|---|---|
| /infer, no token | 401 | test_01 |
| /infer, wrong token | 401 | test_01 |
| /infer, correct token | 200 | test_01 |
| /health/ready, no token | 401 | test_01 |
| /health/ready, correct token | 200 | test_01 |
| /health/live, no token | 401 | test_01 |
| /health/live, correct token | 200 | test_01 |
| App A token against App B | 401 | test_02 |
| old token after recycle | 401 | test_03 |
| new token after recycle | 200 | test_03 |
| token absent from logs/manifest/registry | PASS | test_04 |
| one app auth failure does not affect another | PASS | test_05 |
| simultaneous timeout causes one recycle only | PASS | test_06 |

The REAL worker_runtime process (127.0.0.1) is used as the worker; the
credential is delivered via a credential FILE (dev approximation of
LoadCredential=; the secret is never in argv/env). The REAL systemd path
is proven by the privileged root harness (Fase C).
"""
from __future__ import annotations

import hashlib
import http.client
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(REPO, "examples", "text-classifier")


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _spawn(app_root: str, port: int, cred_file: str):
    cmd = [sys.executable, "-m", "sydeco_lightml_core.worker_runtime",
           "--app-root", app_root, "--port", str(port),
           "--credential-file", cred_file]
    env = dict(os.environ,
               PYTHONPYCACHEPREFIX=tempfile.mkdtemp(prefix="sydeco-authc-pc-"))
    return subprocess.Popen(
        cmd, cwd=REPO, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _req(port: int, method: str, path: str, token: str | None,
         body: bytes | None = None, timeout: float = 2.0):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    headers = {}
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    if body is not None:
        headers["Content-Type"] = "application/json"
    conn.request(method, path, body=body, headers=headers)
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp.status, data


def _wait_ready(port: int, token: str, timeout_s: float = 20) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            status, _ = _req(port, "GET", "/health/ready", token, timeout=1)
            if status == 200:
                return True
        except Exception:
            pass
        time.sleep(0.2)
    return False


class AcceptanceMatrixTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="sydeco-acceptc-")

    def tearDown(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_01_endpoint_matrix(self) -> None:
        secret = "c1" * 32
        cred = os.path.join(self._tmp, "cred")
        with open(cred, "w", encoding="utf-8") as fh:
            fh.write(secret + "\n")
        port = _free_port()
        proc = _spawn(APP, port, cred)
        try:
            self.assertTrue(_wait_ready(port, secret))
            # /infer
            st, _ = _req(port, "POST", "/infer", None,
                         body=json.dumps({"text": "x"}).encode())
            self.assertEqual(st, 401)  # no token
            st, _ = _req(port, "POST", "/infer", "b" * 64,
                         body=json.dumps({"text": "x"}).encode())
            self.assertEqual(st, 401)  # wrong token
            st, data = _req(port, "POST", "/infer", secret,
                            body=json.dumps({"text": "x"}).encode())
            self.assertEqual(st, 200)  # correct token
            self.assertIn(b"result", data)
            # /health/ready
            st, _ = _req(port, "GET", "/health/ready", None)
            self.assertEqual(st, 401)
            st, _ = _req(port, "GET", "/health/ready", secret)
            self.assertEqual(st, 200)
            # /health/live
            st, _ = _req(port, "GET", "/health/live", None)
            self.assertEqual(st, 401)
            st, _ = _req(port, "GET", "/health/live", secret)
            self.assertEqual(st, 200)
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()

    def test_02_app_a_token_against_app_b(self) -> None:
        secret_a, secret_b = "aa" * 32, "bb" * 32
        cred_a = os.path.join(self._tmp, "ca")
        cred_b = os.path.join(self._tmp, "cb")
        for p, s in ((cred_a, secret_a), (cred_b, secret_b)):
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(s + "\n")
        pa, pb = _free_port(), _free_port()
        pa_ = _spawn(APP, pa, cred_a)
        pb_ = _spawn(APP, pb, cred_b)
        try:
            self.assertTrue(_wait_ready(pa, secret_a))
            self.assertTrue(_wait_ready(pb, secret_b))
            st, _ = _req(pb, "GET", "/health/ready", secret_a)
            self.assertEqual(st, 401)  # App A token against App B
            st, _ = _req(pb, "POST", "/infer", secret_a,
                         body=json.dumps({"text": "x"}).encode())
            self.assertEqual(st, 401)
            st, _ = _req(pb, "POST", "/infer", secret_b,
                         body=json.dumps({"text": "x"}).encode())
            self.assertEqual(st, 200)  # App B's own token works
        finally:
            for p in (pa_, pb_):
                if p.poll() is None:
                    p.terminate()
                    try:
                        p.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        p.kill()

    def test_03_old_token_after_recycle_rejected(self) -> None:
        """Recycle = stop the worker generation and start a replacement
        with a FRESH secret; the OLD secret must be rejected."""
        old_secret, new_secret = "o1" * 32, "n1" * 32
        cred1 = os.path.join(self._tmp, "cred1")
        cred2 = os.path.join(self._tmp, "cred2")
        with open(cred1, "w", encoding="utf-8") as fh:
            fh.write(old_secret + "\n")
        with open(cred2, "w", encoding="utf-8") as fh:
            fh.write(new_secret + "\n")
        port = _free_port()
        gen1 = _spawn(APP, port, cred1)
        try:
            self.assertTrue(_wait_ready(port, old_secret))
            st, _ = _req(port, "POST", "/infer", old_secret,
                         body=json.dumps({"text": "x"}).encode())
            self.assertEqual(st, 200)  # old token works on OLD generation
        finally:
            if gen1.poll() is None:
                gen1.terminate()
                try:
                    gen1.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    gen1.kill()
        # replacement generation with the fresh secret
        gen2 = _spawn(APP, port, cred2)
        try:
            self.assertTrue(_wait_ready(port, new_secret))
            st, _ = _req(port, "POST", "/infer", old_secret,
                         body=json.dumps({"text": "x"}).encode())
            self.assertEqual(st, 401)  # old token after recycle -> 401
            st, _ = _req(port, "POST", "/infer", new_secret,
                         body=json.dumps({"text": "x"}).encode())
            self.assertEqual(st, 200)  # new token -> 200
        finally:
            if gen2.poll() is None:
                gen2.terminate()
                try:
                    gen2.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    gen2.kill()

    def test_04_token_absent_from_logs_manifest_registry(self) -> None:
        # P0 day-2 closure (reviewer P0-1): the leakage-scan secret must be
        # runtime-generated so compiled bytecode can never embed it and be
        # reported as a false leakage.
        secret = secrets.token_hex(32)
        cred = os.path.join(self._tmp, "cred")
        with open(cred, "w", encoding="utf-8") as fh:
            fh.write(secret + "\n")
        data_dir = os.path.join(self._tmp, "data")
        os.makedirs(data_dir, exist_ok=True)
        port = _free_port()
        cmd = [sys.executable, "-m", "sydeco_lightml_core.worker_runtime",
               "--app-root", APP, "--port", str(port),
               "--data-dir", data_dir, "--credential-file", cred]
        env = dict(os.environ,
                   PYTHONPYCACHEPREFIX=tempfile.mkdtemp(prefix="sydeco-acceptc-pc-"))
        proc = subprocess.Popen(cmd, cwd=REPO, env=env,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
        try:
            self.assertTrue(_wait_ready(port, secret))
            # trigger an auth failure (audited) + a successful inference
            _req(port, "GET", "/health/ready", "wrongwrongwrongwrong")
            _req(port, "POST", "/infer", secret,
                 body=json.dumps({"text": "x"}).encode())
            # drain stdout/stderr (worker logs) — terminate first, then read
            time.sleep(0.5)
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
            out = proc.stdout.read() if proc.stdout is not None else ""
            err = proc.stderr.read() if proc.stderr is not None else ""
            if proc.stdout is not None:
                proc.stdout.close()
            if proc.stderr is not None:
                proc.stderr.close()
            self.assertNotIn(secret, out)
            self.assertNotIn(secret, err)
            # worker audit JSONL
            audit_path = os.path.join(data_dir, "audit", "audit.jsonl")
            if os.path.exists(audit_path):
                with open(audit_path, "r", encoding="utf-8") as fh:
                    blob = fh.read()
                self.assertNotIn(secret, blob)
            # manifest + whole repo tree
            hits = []
            for root, dirs, files in os.walk(REPO):
                if ".git" in root:
                    continue
                for fn in files:
                    p = os.path.join(root, fn)
                    try:
                        with open(p, "rb") as fh:
                            if secret.encode() in fh.read():
                                hits.append(p)
                    except OSError:
                        continue
            self.assertEqual(hits, [])
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()

    def test_05_one_auth_failure_does_not_affect_another(self) -> None:
        secret_a, secret_b = "e1" * 32, "e2" * 32
        cred_a = os.path.join(self._tmp, "ca")
        cred_b = os.path.join(self._tmp, "cb")
        for p, s in ((cred_a, secret_a), (cred_b, secret_b)):
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(s + "\n")
        pa, pb = _free_port(), _free_port()
        pa_ = _spawn(APP, pa, cred_a)
        pb_ = _spawn(APP, pb, cred_b)
        try:
            self.assertTrue(_wait_ready(pa, secret_a))
            self.assertTrue(_wait_ready(pb, secret_b))
            # many failures against App B (wrong token)
            for _ in range(3):
                st, _ = _req(pb, "GET", "/health/ready", "z" * 64)
                self.assertEqual(st, 401)
            # App A still serves fine
            st, _ = _req(pa, "POST", "/infer", secret_a,
                         body=json.dumps({"text": "x"}).encode())
            self.assertEqual(st, 200)
            # App B's OWN token still works after its failures
            st, _ = _req(pb, "POST", "/infer", secret_b,
                         body=json.dumps({"text": "x"}).encode())
            self.assertEqual(st, 200)
        finally:
            for p in (pa_, pb_):
                if p.poll() is None:
                    p.terminate()
                    try:
                        p.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        p.kill()

    def test_06_simultaneous_timeout_single_recycle(self) -> None:
        """Acceptance row 13 — proven deterministically here (fast variant)
        and in detail by test_channel_auth.test_01 (Fase A, P1)."""
        import types

        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        from sydeco_lightml_core.audit import JsonlAuditBackend
        from sydeco_lightml_core.health import ReadinessStore
        from sydeco_lightml_core.worker import (
            InferenceTimeout,
            SystemdTransientWorkerHost,
        )

        class _Server:
            def __init__(self) -> None:
                self.httpd = None
                self.port = 0
                self._lock = threading.Lock()
                self._n = 0

            def _handler(self):
                srv = self

                class H(BaseHTTPRequestHandler):
                    protocol_version = "HTTP/1.1"

                    def log_message(self, format, *args):  # noqa: A002
                        pass

                    def _send(self, code, obj):
                        try:
                            body = json.dumps(obj).encode()
                            self.send_response(code)
                            self.send_header("Content-Length", str(len(body)))
                            self.end_headers()
                            self.wfile.write(body)
                        except OSError:
                            pass

                    def do_POST(self):
                        with srv._lock:
                            srv._n += 1
                            n = srv._n
                        if n <= 2:
                            time.sleep(2.0)
                        self._send(200, {"result": "ok"})

                    def do_GET(self):
                        self._send(200, {"status": "ready"})

                return H

            def start(self):
                self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
                self.port = self.httpd.server_address[1]
                threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

            def stop(self):
                if self.httpd:
                    self.httpd.shutdown()
                    self.httpd.server_close()

        import subprocess as _sp

        server = _Server()
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
            "port": server.port, "user": "", "cwd": "", "pythonpath": "",
        }
        calls = []

        class _Fake:
            def run(self, argv, **kw):
                calls.append(list(argv))
                if argv[0] == "systemd-run":
                    if len([c for c in calls if c[0] == "systemd-run"]) >= 2:
                        time.sleep(0.8)
                    return types.SimpleNamespace(returncode=0, stdout="", stderr="")
                return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        fake = _Fake()
        orig = _sp.run
        _sp.run = fake.run
        self.addCleanup(host.shutdown)
        self.addCleanup(server.stop)
        self.addCleanup(lambda: setattr(_sp, "run", orig))

        host.start("app-a", "1.0.0", None, context)
        barrier = threading.Barrier(2)
        got = []

        def _a():
            barrier.wait()
            try:
                host.infer({"text": "STALL"}, "r")
            except InferenceTimeout:
                got.append("timeout")
            except Exception as e:  # noqa: BLE001
                got.append(type(e).__name__)

        ts = [threading.Thread(target=_a) for _ in range(2)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(timeout=10)
        deadline = time.time() + 6
        while time.time() < deadline and not readiness.is_ready("app-a"):
            time.sleep(0.05)
        self.assertEqual(got, ["timeout", "timeout"])
        run_calls = [c for c in calls if c[0] == "systemd-run"]
        self.assertEqual(len(run_calls), 2,
                         "exactly one recycle: initial + one relaunch")
        entries = []
        ap = os.path.join(self._tmp, "audit", "audit.jsonl")
        if os.path.exists(ap):
            with open(ap, "r", encoding="utf-8") as fh:
                entries = [json.loads(l) for l in fh if l.strip()]
        restarts = [e for e in entries
                    if e.get("action") == "WORKER_RESTART"
                    and e.get("detail") == "timeout recycle"]
        self.assertEqual(len(restarts), 1)


if __name__ == "__main__":
    unittest.main()
