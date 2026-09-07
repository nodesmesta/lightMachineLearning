"""Phase 2 / Day 4B - K5 systemd production-path parity RED tests.

These tests exercise SystemdTransientWorkerHost directly. The systemd process
manager is faked, but the Core-side production host, loopback HTTP stream, and
credential forwarding path are the real code under test.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import tempfile
import threading
import time
import types
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from sydeco_lightml_core.audit import JsonlAuditBackend
from sydeco_lightml_core.health import ReadinessStore
from sydeco_lightml_core.worker import (
    StreamBackpressure,
    StreamRestarted,
    StreamTimeout,
    SystemdTransientWorkerHost,
)


class _FakeSubprocess:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
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


class _ScriptedWorkerServer:
    def __init__(self, script: Callable[[BaseHTTPRequestHandler, "_ScriptedWorkerServer"], None]) -> None:
        self.httpd: ThreadingHTTPServer | None = None
        self.port = 0
        self.seen: list[dict[str, str]] = []
        self.post_started = threading.Event()
        self.first_chunk_sent = threading.Event()
        self.client_disconnected = threading.Event()
        self.continue_after_restart = threading.Event()
        self._script = script
        self._lock = threading.Lock()

    def _handler_factory(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format, *args):  # noqa: A002
                pass

            def _send_json(self, code: int, obj: dict[str, Any]) -> None:
                body = json.dumps(obj).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _send_stream_headers(self) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson")
                self.send_header("Connection", "close")
                self.end_headers()

            def _write_frame(self, obj: dict[str, Any]) -> None:
                self.wfile.write(json.dumps(obj).encode("utf-8") + b"\n")
                self.wfile.flush()
                if not server.first_chunk_sent.is_set():
                    server.first_chunk_sent.set()

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
                server.post_started.set()
                try:
                    server._script(self, server)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    server.client_disconnected.set()

        return Handler

    def start(self) -> None:
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), self._handler_factory())
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self) -> None:
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()


def _sleep_before_first_chunk(handler: BaseHTTPRequestHandler, _server: _ScriptedWorkerServer) -> None:
    handler._send_stream_headers()
    time.sleep(0.35)
    handler._write_frame({"type": "chunk", "data": {"label": "too-late"}})
    handler._write_frame({"type": "completed"})


def _stall_after_first_chunk(handler: BaseHTTPRequestHandler, _server: _ScriptedWorkerServer) -> None:
    handler._send_stream_headers()
    handler._write_frame({"type": "chunk", "data": {"label": "first"}})
    time.sleep(0.35)
    handler._write_frame({"type": "chunk", "data": {"label": "too-late"}})
    handler._write_frame({"type": "completed"})


def _long_but_active_stream(handler: BaseHTTPRequestHandler, _server: _ScriptedWorkerServer) -> None:
    handler._send_stream_headers()
    for i in range(10):
        handler._write_frame({"type": "chunk", "data": {"label": str(i)}})
        time.sleep(0.05)
    handler._write_frame({"type": "completed"})


def _adapter_error_frame(handler: BaseHTTPRequestHandler, _server: _ScriptedWorkerServer) -> None:
    handler._send_stream_headers()
    handler._write_frame({"type": "chunk", "data": {"label": "first"}})
    handler._write_frame({
        "type": "error",
        "error": {"code": "500", "message": "internal error"},
    })


def _abnormal_eof_after_chunk(handler: BaseHTTPRequestHandler, _server: _ScriptedWorkerServer) -> None:
    handler._send_stream_headers()
    handler._write_frame({"type": "chunk", "data": {"label": "first"}})
    handler.close_connection = True


def _slow_stream(handler: BaseHTTPRequestHandler, _server: _ScriptedWorkerServer) -> None:
    handler._send_stream_headers()
    handler._write_frame({"type": "chunk", "data": {"label": "first"}})
    time.sleep(1.0)
    handler._write_frame({"type": "chunk", "data": {"label": "late"}})
    handler._write_frame({"type": "completed"})


def _block_after_first_chunk(handler: BaseHTTPRequestHandler, server: _ScriptedWorkerServer) -> None:
    handler._send_stream_headers()
    handler._write_frame({"type": "chunk", "data": {"label": "first"}})
    server.continue_after_restart.wait(timeout=2.0)


def _eof_after_restart(handler: BaseHTTPRequestHandler, server: _ScriptedWorkerServer) -> None:
    handler._send_stream_headers()
    handler._write_frame({"type": "chunk", "data": {"label": "first"}})
    server.continue_after_restart.wait(timeout=2.0)
    handler.close_connection = True


def _transport_failure_after_restart(handler: BaseHTTPRequestHandler, server: _ScriptedWorkerServer) -> None:
    handler._send_stream_headers()
    handler._write_frame({"type": "chunk", "data": {"label": "first"}})
    server.continue_after_restart.wait(timeout=2.0)
    try:
        handler.connection.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    handler.connection.close()
    handler.close_connection = True


class SystemdStreamingParityTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="sydeco-stream-systemd-parity-")
        self.server: _ScriptedWorkerServer | None = None

    def tearDown(self) -> None:
        if self.server is not None:
            self.server.stop()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _start_host(
        self,
        script: Callable[[BaseHTTPRequestHandler, _ScriptedWorkerServer], None],
        *,
        limits: dict[str, Any] | None = None,
    ) -> tuple[SystemdTransientWorkerHost, _FakeSubprocess, _ScriptedWorkerServer]:
        import subprocess as _subprocess

        self.server = _ScriptedWorkerServer(script)
        self.server.start()
        audit = JsonlAuditBackend(os.path.join(self._tmp, "audit"))
        readiness = ReadinessStore()
        host = SystemdTransientWorkerHost(
            readiness=readiness,
            audit=audit,
            poll_interval=0.05,
            ready_wait=2.0,
        )
        resource_limits = {
            "inference_timeout": 1.0,
            "stream_first_chunk_timeout": 0.1,
            "stream_idle_timeout": 0.1,
            "stream_total_timeout": 0.15,
            "stream_buffer_size": 1,
            "stream_backpressure_timeout": 0.05,
        }
        if limits:
            resource_limits.update(limits)
        context = {
            "app_root": self._tmp,
            "config": {"resource_limits": resource_limits},
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
        return host, fake, self.server

    @staticmethod
    def _restore_subprocess(mod, orig) -> None:
        mod.run = orig

    def _start_next_reader_after_read_enters(self, stream):
        import http.client

        original_readline = http.client.HTTPResponse.readline
        read_entered = threading.Event()
        read_marked = False
        read_lock = threading.Lock()

        def wrapped_readline(response, *args, **kwargs):
            nonlocal read_marked
            with read_lock:
                if not read_marked:
                    read_marked = True
                    read_entered.set()
            return original_readline(response, *args, **kwargs)

        http.client.HTTPResponse.readline = wrapped_readline
        self.addCleanup(self._restore_readline, http.client, original_readline)

        result: dict[str, Any] = {}

        def read_next() -> None:
            try:
                result["value"] = next(stream)
            except BaseException as exc:
                result["exception"] = exc

        reader = threading.Thread(target=read_next)
        reader.start()
        return reader, read_entered, result

    def test_systemd_first_chunk_timeout_uses_stream_bound(self) -> None:
        host, _fake, _server = self._start_host(
            _sleep_before_first_chunk,
            limits={
                "inference_timeout": 1.0,
                "stream_first_chunk_timeout": 0.1,
                "stream_idle_timeout": 1.0,
                "stream_total_timeout": 1.0,
            },
        )

        with self.assertRaises(StreamTimeout) as raised:
            list(host.stream({"text": "hello"}, "r-first"))

        self.assertEqual(raised.exception.reason, "first_chunk")

    def test_systemd_idle_timeout_uses_stream_bound(self) -> None:
        host, _fake, _server = self._start_host(
            _stall_after_first_chunk,
            limits={
                "inference_timeout": 1.0,
                "stream_first_chunk_timeout": 1.0,
                "stream_idle_timeout": 0.1,
                "stream_total_timeout": 1.0,
            },
        )
        stream = host.stream({"text": "hello"}, "r-idle")

        self.assertEqual(next(stream), {"label": "first"})
        with self.assertRaises(StreamTimeout) as raised:
            next(stream)

        self.assertEqual(raised.exception.reason, "idle")

    def test_systemd_total_timeout_uses_monotonic_absolute_deadline(self) -> None:
        host, _fake, _server = self._start_host(
            _long_but_active_stream,
            limits={
                "inference_timeout": 1.0,
                "stream_first_chunk_timeout": 1.0,
                "stream_idle_timeout": 1.0,
                "stream_total_timeout": 0.15,
            },
        )

        with self.assertRaises(StreamTimeout) as raised:
            list(host.stream({"text": "hello"}, "r-total"))

        self.assertEqual(raised.exception.reason, "total")

    def test_systemd_adapter_error_frame_is_not_returned_as_chunk_data(self) -> None:
        host, _fake, _server = self._start_host(
            _adapter_error_frame,
            limits={
                "inference_timeout": 1.0,
                "stream_first_chunk_timeout": 1.0,
                "stream_idle_timeout": 1.0,
                "stream_total_timeout": 1.0,
            },
        )
        stream = host.stream({"text": "hello"}, "r-error")

        self.assertEqual(next(stream), {"label": "first"})
        with self.assertRaises(RuntimeError):
            next(stream)

    def test_systemd_abnormal_eof_before_terminal_frame_fails_closed(self) -> None:
        host, _fake, _server = self._start_host(
            _abnormal_eof_after_chunk,
            limits={
                "inference_timeout": 1.0,
                "stream_first_chunk_timeout": 1.0,
                "stream_idle_timeout": 1.0,
                "stream_total_timeout": 1.0,
            },
        )
        stream = host.stream({"text": "hello"}, "r-eof")

        self.assertEqual(next(stream), {"label": "first"})
        with self.assertRaises(RuntimeError):
            next(stream)

    def test_systemd_restart_during_active_stream_raises_stream_restarted(self) -> None:
        host, _fake, server = self._start_host(
            _slow_stream,
            limits={
                "inference_timeout": 2.0,
                "stream_first_chunk_timeout": 1.0,
                "stream_idle_timeout": 2.0,
                "stream_total_timeout": 2.0,
            },
        )
        stream = host.stream({"text": "hello"}, "r-restart")

        self.assertEqual(next(stream), {"label": "first"})
        self.assertTrue(server.first_chunk_sent.wait(timeout=1.0))
        host.restart("app-a")
        with self.assertRaises(StreamRestarted):
            next(stream)

    def test_systemd_concurrent_restart_during_blocked_read_raises_stream_restarted(self) -> None:
        host, _fake, server = self._start_host(
            _block_after_first_chunk,
            limits={
                "inference_timeout": 2.0,
                "stream_first_chunk_timeout": 1.0,
                "stream_idle_timeout": 0.2,
                "stream_total_timeout": 2.0,
            },
        )
        stream = host.stream({"text": "hello"}, "r-blocked-restart")
        self.assertEqual(next(stream), {"label": "first"})

        reader, read_entered, result = self._start_next_reader_after_read_enters(stream)
        self.assertTrue(read_entered.wait(timeout=1.0))
        host.restart("app-a")
        reader.join(timeout=2.0)
        server.continue_after_restart.set()

        self.assertFalse(reader.is_alive())
        self.assertIsInstance(result.get("exception"), StreamRestarted)

    def test_systemd_restart_followed_by_eof_raises_stream_restarted(self) -> None:
        host, _fake, server = self._start_host(
            _eof_after_restart,
            limits={
                "inference_timeout": 2.0,
                "stream_first_chunk_timeout": 1.0,
                "stream_idle_timeout": 1.0,
                "stream_total_timeout": 2.0,
            },
        )
        stream = host.stream({"text": "hello"}, "r-restart-eof")

        self.assertEqual(next(stream), {"label": "first"})
        reader, read_entered, result = self._start_next_reader_after_read_enters(stream)
        self.assertTrue(read_entered.wait(timeout=1.0))
        host.restart("app-a")
        server.continue_after_restart.set()
        reader.join(timeout=2.0)

        self.assertFalse(reader.is_alive())
        self.assertIsInstance(result.get("exception"), StreamRestarted)

    def test_systemd_restart_followed_by_transport_failure_raises_stream_restarted(self) -> None:
        host, _fake, server = self._start_host(
            _transport_failure_after_restart,
            limits={
                "inference_timeout": 2.0,
                "stream_first_chunk_timeout": 1.0,
                "stream_idle_timeout": 1.0,
                "stream_total_timeout": 2.0,
            },
        )
        stream = host.stream({"text": "hello"}, "r-restart-transport")

        self.assertEqual(next(stream), {"label": "first"})
        reader, read_entered, result = self._start_next_reader_after_read_enters(stream)
        self.assertTrue(read_entered.wait(timeout=1.0))
        host.restart("app-a")
        server.continue_after_restart.set()
        reader.join(timeout=2.0)

        self.assertFalse(reader.is_alive())
        self.assertIsInstance(result.get("exception"), StreamRestarted)

    def test_systemd_client_cancellation_does_not_leave_worker_busy(self) -> None:
        host, fake, _server = self._start_host(
            _slow_stream,
            limits={
                "inference_timeout": 2.0,
                "stream_first_chunk_timeout": 1.0,
                "stream_idle_timeout": 2.0,
                "stream_total_timeout": 2.0,
            },
        )
        stream = host.stream({"text": "hello"}, "r-cancel")

        self.assertEqual(next(stream), {"label": "first"})
        stream.close()
        time.sleep(0.1)

        systemctl_calls = [call for call in fake.calls if call and call[0] == "systemctl"]
        stop_or_kill = [
            call for call in systemctl_calls
            if len(call) > 1 and call[1] in {"stop", "kill"}
        ]
        self.assertTrue(stop_or_kill, fake.calls)

    def test_systemd_slow_consumer_has_bounded_behavior(self) -> None:
        host, _fake, _server = self._start_host(
            _long_but_active_stream,
            limits={
                "inference_timeout": 1.0,
                "stream_first_chunk_timeout": 1.0,
                "stream_idle_timeout": 1.0,
                "stream_total_timeout": 1.0,
                "stream_buffer_size": 1,
                "stream_backpressure_timeout": 0.05,
            },
        )
        stream = host.stream({"text": "hello"}, "r-backpressure")

        self.assertEqual(next(stream), {"label": "0"})
        time.sleep(0.2)
        with self.assertRaises(StreamBackpressure):
            next(stream)

    @staticmethod
    def _restore_readline(mod, orig) -> None:
        mod.HTTPResponse.readline = orig


if __name__ == "__main__":
    unittest.main()
