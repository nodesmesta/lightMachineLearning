"""Phase 2 / Day 4 — K5 systemd streaming credential RED tests.

These tests pin the production-path requirement before implementation:
SystemdTransientWorkerHost.stream() must forward streaming requests to the
worker using the current per-generation Bearer credential, just like ordinary
infer(), and a restart must rotate the internal worker credential.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import time
import types
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from sydeco_lightml_core.audit import JsonlAuditBackend
from sydeco_lightml_core.health import ReadinessStore
from sydeco_lightml_core.worker import SystemdTransientWorkerHost


class _StreamingWorkerServer:
    def __init__(self) -> None:
        self.httpd: ThreadingHTTPServer | None = None
        self.port = 0
        self.seen = []
        self._lock = threading.Lock()

    def _handler_factory(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format, *args):  # noqa: A002
                pass

            def _send_json(self, code, obj):
                body = json.dumps(obj).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):  # noqa: N802
                with server._lock:
                    server.seen.append({
                        "path": self.path,
                        "auth": self.headers.get("Authorization", ""),
                    })
                self._send_json(200, {"status": "ready"})

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                with server._lock:
                    server.seen.append({
                        "path": self.path,
                        "auth": self.headers.get("Authorization", ""),
                        "accept": self.headers.get("Accept", ""),
                        "body": body.decode("utf-8"),
                    })
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(json.dumps({"data": {"label": "systemd-stream"}}).encode("utf-8") + b"\n")
                self.close_connection = True

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
    def __init__(self) -> None:
        self.calls = []
        self._lock = threading.Lock()

    def run(self, argv, **kwargs):
        with self._lock:
            self.calls.append(list(argv))
        if argv and argv[0] == "systemd-run":
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        if argv and argv[0] == "systemctl":
            if len(argv) > 1 and argv[1] == "show":
                if "--value" in argv:
                    return types.SimpleNamespace(returncode=0, stdout="inactive\n", stderr="")
                return types.SimpleNamespace(
                    returncode=0,
                    stdout="ActiveState=active\nResult=\nNRestarts=0\n",
                    stderr="",
                )
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")


class SystemdStreamingAuthTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="sydeco-stream-systemd-")
        self.server = _StreamingWorkerServer()
        self.server.start()

    def tearDown(self) -> None:
        self.server.stop()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _start_host(self) -> tuple[SystemdTransientWorkerHost, _FakeSubprocess]:
        import subprocess as _subprocess

        audit = JsonlAuditBackend(os.path.join(self._tmp, "audit"))
        readiness = ReadinessStore()
        host = SystemdTransientWorkerHost(
            readiness=readiness,
            audit=audit,
            poll_interval=0.05,
            ready_wait=2.0,
        )
        context = {
            "app_root": self._tmp,
            "config": {"resource_limits": {"inference_timeout": 1.0}},
            "data_dir": os.path.join(self._tmp, "data"),
            "port": self.server.port,
            "user": "",
            "cwd": "",
            "pythonpath": "",
            "credential_dir": os.path.join(self._tmp, "secrets"),
        }
        fake = _FakeSubprocess()
        orig_run = _subprocess.run
        _subprocess.run = fake.run
        self.addCleanup(self._restore_subprocess, _subprocess, orig_run)
        self.addCleanup(host.shutdown)
        host.start("app-a", "1.0.0", None, context)
        return host, fake

    @staticmethod
    def _restore_subprocess(mod, orig) -> None:
        mod.run = orig

    def test_systemd_stream_uses_current_generation_bearer(self) -> None:
        host, _fake = self._start_host()
        current_secret = host._secret
        self.assertIsNotNone(current_secret)
        assert current_secret is not None

        chunks = list(host.stream({"text": "hello"}, "r-stream"))

        self.assertEqual(chunks, [{"label": "systemd-stream"}])
        stream_calls = [entry for entry in self.server.seen if entry.get("path") == "/stream"]
        self.assertEqual(len(stream_calls), 1, self.server.seen)
        self.assertEqual(stream_calls[0]["auth"], "Bearer " + current_secret)
        self.assertIn("application/x-ndjson", stream_calls[0]["accept"])
        self.assertEqual(json.loads(stream_calls[0]["body"]), {"text": "hello"})

    def test_systemd_stream_uses_rotated_secret_after_restart(self) -> None:
        host, _fake = self._start_host()
        old_secret = host._secret
        self.assertIsNotNone(old_secret)
        assert old_secret is not None

        host.restart("app-a")
        new_secret = host._secret
        self.assertIsNotNone(new_secret)
        assert new_secret is not None
        self.assertNotEqual(old_secret, new_secret)

        chunks = list(host.stream({"text": "after-restart"}, "r-stream-2"))

        self.assertEqual(chunks, [{"label": "systemd-stream"}])
        stream_calls = [entry for entry in self.server.seen if entry.get("path") == "/stream"]
        self.assertEqual(len(stream_calls), 1, self.server.seen)
        self.assertEqual(stream_calls[0]["auth"], "Bearer " + new_secret)
        self.assertNotEqual(stream_calls[0]["auth"], "Bearer " + old_secret)


if __name__ == "__main__":
    unittest.main()
