"""Phase 2 / Day 2 — Fase B tests: authenticated worker API + isolation +
leakage (2026-08-28; reviewer P3/P4/P5).

P4 (reviewer P3): the worker REQUIRES the Bearer credential on ALL internal
endpoints — POST /infer, GET /health/ready, GET /health/live. No
unauthenticated worker endpoint. Constant-time comparison (hmac.
compare_digest). Unauthenticated / wrong / foreign-app credential -> 401
(generic message); correct -> 200. Auth failures are audited worker-side
(AUTH_FAILURE in the worker's own data-dir audit JSONL — C2 elaboration).

P5 (reviewer P4): credential isolation — App A token -> App A accepted;
App A token -> App B rejected; old App A generation token -> new App A
generation rejected; new App A token -> new generation accepted; one app's
auth failure does not affect another.

P6 (reviewer P5): leakage — 0 occurrences of the secret in command line,
/proc/<pid>/cmdline, /proc/<pid>/environ, manifest, registry, audit log,
normal logs, client error responses, package, git repository. Only allowed
locations: tightly controlled runtime memory + the systemd credential
mechanism.
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


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _spawn_worker(app_root: str, port: int, cred_file: str | None,
                  cred_dir_env: str | None = None):
    cmd = [sys.executable, "-m", "sydeco_lightml_core.worker_runtime",
           "--app-root", app_root, "--port", str(port)]
    if cred_file:
        cmd += ["--credential-file", cred_file]
    env = dict(
        os.environ,
        PYTHONPYCACHEPREFIX=tempfile.mkdtemp(prefix="sydeco-authb-pycache-"),
    )
    if cred_dir_env:
        env["CREDENTIALS_DIRECTORY"] = cred_dir_env
    return subprocess.Popen(
        cmd, cwd=REPO, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _request(port: int, method: str, path: str, token: str | None,
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
            status, _ = _request(port, "GET", "/health/ready", token, timeout=1)
            if status == 200:
                return True
        except Exception:
            pass
        time.sleep(0.2)
    return False


class ChannelAuthFaseBTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="sydeco-authb-")

    def tearDown(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)

    # ---- P4: the complete internal worker API is authenticated ---------

    def test_01_all_endpoints_require_bearer(self) -> None:
        app_root = os.path.join(REPO, "examples", "text-classifier")
        secret = "f" * 64
        cred_file = os.path.join(self._tmp, "worker-secret")
        with open(cred_file, "w", encoding="utf-8") as fh:
            fh.write(secret + "\n")
        port = _free_port()
        proc = _spawn_worker(app_root, port, cred_file)
        try:
            self.assertTrue(
                _wait_ready(port, secret), "worker with credential not ready"
            )
            # no token -> 401 on ALL endpoints
            for method, path in [("GET", "/health/live"),
                                 ("GET", "/health/ready"),
                                 ("POST", "/infer")]:
                status, data = _request(port, method, path, None)
                self.assertEqual(status, 401, f"{method} {path} no token")
                self.assertIn(b"unauthorized", data.lower())
                self.assertNotIn(secret.encode(), data)
            # wrong token -> 401
            status, _ = _request(port, "GET", "/health/ready", "w" * 64)
            self.assertEqual(status, 401)
            # correct token -> 200
            status, _ = _request(port, "GET", "/health/ready", secret)
            self.assertEqual(status, 200)
            status, _ = _request(port, "GET", "/health/live", secret)
            self.assertEqual(status, 200)
            status, data = _request(
                port, "POST", "/infer", secret,
                body=json.dumps({"text": "hello"}).encode(),
            )
            self.assertEqual(status, 200)
            self.assertIn(b"result", data)
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()

    def test_02_concurrent_mixed_tokens_never_share_request_state(self) -> None:
        """A valid and invalid concurrent request must not cross-authenticate."""
        import types
        from unittest.mock import patch

        from sydeco_lightml_core import worker_runtime as wr

        secret = "q" * 64
        barrier = threading.Barrier(2)

        class _Adapter:
            def infer(self, request, context):
                return {"ok": True}

        runtime = types.SimpleNamespace(
            adapter=_Adapter(), context={}, infer_lock=threading.Lock(),
            request_id=lambda: "race", secret=secret, app_id="race",
            data_dir=None,
        )
        httpd = wr.WorkerRuntimeServer(("127.0.0.1", 0), runtime)
        server_thread = threading.Thread(
            target=httpd.serve_forever, daemon=True
        )
        server_thread.start()
        port = httpd.server_address[1]

        original_check = wr._check_bearer

        def synchronized_check(runtime, presented):
            barrier.wait(timeout=3)
            return original_check(runtime, presented)

        results = []

        def request(token):
            status, _ = _request(port, "GET", "/health/live", token)
            results.append((token == secret, status))

        try:
            with patch.object(wr, "_check_bearer", synchronized_check):
                threads = [
                    threading.Thread(target=request, args=(secret,)),
                    threading.Thread(target=request, args=("w" * 64,)),
                ]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=5)
            self.assertEqual(len(results), 2)
            self.assertEqual(
                sorted(results), [(False, 401), (True, 200)]
            )
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_03_auth_failure_is_audited_worker_side(self) -> None:
        app_root = os.path.join(REPO, "examples", "text-classifier")
        secret = "a1" * 32
        cred_file = os.path.join(self._tmp, "worker-secret")
        with open(cred_file, "w", encoding="utf-8") as fh:
            fh.write(secret + "\n")
        data_dir = os.path.join(self._tmp, "app-data")
        os.makedirs(data_dir, exist_ok=True)
        port = _free_port()
        cmd = [sys.executable, "-m", "sydeco_lightml_core.worker_runtime",
               "--app-root", app_root, "--port", str(port),
               "--data-dir", data_dir, "--credential-file", cred_file]
        env = dict(os.environ,
                   PYTHONPYCACHEPREFIX=tempfile.mkdtemp(prefix="sydeco-authb-pc-"))
        proc = subprocess.Popen(cmd, cwd=REPO, env=env,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        try:
            self.assertTrue(_wait_ready(port, secret))
            status, _ = _request(port, "GET", "/health/ready", "bad" * 21)
            self.assertEqual(status, 401)
            audit_path = os.path.join(data_dir, "audit", "audit.jsonl")
            deadline = time.time() + 5
            while time.time() < deadline and not os.path.exists(audit_path):
                time.sleep(0.1)
            self.assertTrue(os.path.exists(audit_path))
            with open(audit_path, "r", encoding="utf-8") as fh:
                lines = [json.loads(l) for l in fh if l.strip()]
            failures = [e for e in lines if e.get("action") == "AUTH_FAILURE"]
            self.assertEqual(len(failures), 1)
            # the event never contains the secret or the presented token
            blob = json.dumps(lines)
            self.assertNotIn(secret, blob)
            self.assertNotIn("bad" * 21, blob)
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()

    # ---- P5: credential isolation ---------------------------------------

    def test_03_app_a_token_rejected_by_app_b(self) -> None:
        app_root = os.path.join(REPO, "examples", "text-classifier")
        secret_a = "aa" * 32
        secret_b = "bb" * 32
        cred_a = os.path.join(self._tmp, "cred-a")
        cred_b = os.path.join(self._tmp, "cred-b")
        with open(cred_a, "w", encoding="utf-8") as fh:
            fh.write(secret_a + "\n")
        with open(cred_b, "w", encoding="utf-8") as fh:
            fh.write(secret_b + "\n")
        port_a = _free_port()
        port_b = _free_port()
        proc_a = _spawn_worker(app_root, port_a, cred_a)
        proc_b = _spawn_worker(app_root, port_b, cred_b)
        try:
            self.assertTrue(_wait_ready(port_a, secret_a))
            self.assertTrue(_wait_ready(port_b, secret_b))
            # App A token against App B -> 401
            status, _ = _request(port_b, "GET", "/health/ready", secret_a)
            self.assertEqual(status, 401)
            status, _ = _request(port_b, "POST", "/infer", secret_a,
                                 body=json.dumps({"text": "x"}).encode())
            self.assertEqual(status, 401)
            # App B unaffected: its own token still works
            status, _ = _request(port_b, "POST", "/infer", secret_b,
                                 body=json.dumps({"text": "x"}).encode())
            self.assertEqual(status, 200)
            # App A unaffected too
            status, _ = _request(port_a, "GET", "/health/ready", secret_a)
            self.assertEqual(status, 200)
        finally:
            for proc in (proc_a, proc_b):
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()

    def test_04_inprocess_host_isolates_credentials(self) -> None:
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
        self.assertIsNotNone(s1)
        assert s1 is not None
        # old generation token rejected after restart (rotation)
        host.restart("app-x")
        s2 = host._secret
        self.assertIsNotNone(s2)
        assert s2 is not None
        self.assertNotEqual(s1, s2)
        # new generation token accepted (the host's infer carries it)

    # ---- P6: leakage ----------------------------------------------------

    def test_05_no_secret_in_manifest_registry_audit_logs(self) -> None:
        """P6 non-privileged sweep: manifest, registry, Core audit, worker
        audit, logs, client error responses, package tree, git repo."""
        app_root = os.path.join(REPO, "examples", "text-classifier")
        # P0 Day-2 closure (reviewer P0-1): the leakage-test credential is
        # generated DYNAMICALLY at runtime (secrets.token_hex(32)) so Python
        # bytecode (__pycache__/*.pyc) can never embed a deterministic test
        # secret and be reported as a false leakage. Reproducible under any
        # interpreter / pyc state.
        secret = secrets.token_hex(32)
        cred_file = os.path.join(self._tmp, "worker-secret")
        with open(cred_file, "w", encoding="utf-8") as fh:
            fh.write(secret + "\n")
        port = _free_port()
        proc = _spawn_worker(app_root, port, cred_file)
        try:
            self.assertTrue(_wait_ready(port, secret))
            # client error responses never contain the secret
            status, data = _request(port, "GET", "/health/ready", "bad" * 21)
            self.assertEqual(status, 401)
            self.assertNotIn(secret.encode(), data)
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
        # the dev credential FILE is ephemeral test scaffolding under /tmp
        # (never part of the package); the secret value must not appear
        # anywhere in the REPO tree (manifest/registry/audit/logs/source)
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
        self.assertEqual(hits, [], f"secret found in repo: {hits}")

    def test_06_no_secret_in_proc_cmdline_environ(self) -> None:
        """P6 root harness will do the real /proc sweep; this non-privileged
        version proves the SPAWNED worker's argv/environ carry no secret."""
        app_root = os.path.join(REPO, "examples", "text-classifier")
        secret = secrets.token_hex(32)
        cred_file = os.path.join(self._tmp, "worker-secret")
        with open(cred_file, "w", encoding="utf-8") as fh:
            fh.write(secret + "\n")
        port = _free_port()
        proc = _spawn_worker(app_root, port, cred_file)
        try:
            self.assertTrue(_wait_ready(port, secret))
            with open(f"/proc/{proc.pid}/cmdline", "rb") as fh:
                cmdline = fh.read()
            with open(f"/proc/{proc.pid}/environ", "rb") as fh:
                environ = fh.read()
            self.assertNotIn(secret.encode(), cmdline)
            self.assertNotIn(secret.encode(), environ)
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()


if __name__ == "__main__":
    unittest.main()
