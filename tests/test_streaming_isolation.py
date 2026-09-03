"""Phase 2 / Day 4 — K5 streaming cross-app isolation RED tests.

Reviewer P6 requires streaming to preserve the isolation guarantees already
accepted for Day 2 and Day 3B:
- App A streaming does not block App B normal inference;
- App A streaming does not block App B streaming;
- one cancelled stream does not damage another active stream;
- one timed-out stream causes only the appropriate worker action on that app;
- worker recycle during a stream causes deterministic stream termination;
- a new worker generation resumes normal service afterwards.

Each test runs TWO independent streaming apps under ONE CoreService and one
threaded HTTP server, so interference (or its absence) is observable
end-to-end through the public edge.
"""
from __future__ import annotations

import hashlib
import json
import os
import pickle
import shutil
import socket
import tempfile
import time
import unittest
from typing import Any

from sydeco_lightml_core.core import CoreService
from tests._http_harness import HttpHarness
from tests._signing import sign_manifest

DEFAULT_LIMITS = {
    "max_memory": 1073741824,
    "max_cpu": 100,
    "inference_timeout": 2.0,
    "stream_first_chunk_timeout": 1.0,
    "stream_idle_timeout": 1.0,
    "stream_total_timeout": 8.0,
    "concurrency": 1,
}

ADAPTER_A = """\
import time

class Adapter:
    def initialize(self, context):
        pass

    def infer(self, request, context):
        return {"label": "a-infer"}

    def stream(self, request, context):
        for i in range(60):
            yield {"label": "a-%d" % i}
            time.sleep(0.02)
"""

ADAPTER_B = """\
import time

class Adapter:
    def initialize(self, context):
        pass

    def infer(self, request, context):
        return {"label": "b-infer"}

    def stream(self, request, context):
        for i in range(30):
            yield {"label": "b-%d" % i}
            time.sleep(0.02)
"""

ADAPTER_A_SLOW_FIRST_CHUNK = """\
import time

class Adapter:
    def initialize(self, context):
        pass

    def infer(self, request, context):
        return {"label": "a-infer"}

    def stream(self, request, context):
        time.sleep(0.35)
        yield {"label": "too-late"}
"""


class StreamingIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="sydeco-stream-iso-")

    def tearDown(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)

    # ---- helpers -------------------------------------------------------

    def _add_app(
        self,
        core: CoreService,
        app_id: str,
        adapter_src: str,
        resource_limits: dict[str, Any] | None = None,
    ) -> str:
        """Install + start one streaming app; return its public token."""
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
                        "sha256": hashlib.sha256(adapter_src.encode("utf-8")).hexdigest(),
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
            "resource_limits": resource_limits or DEFAULT_LIMITS,
            "dependencies": [],
        }
        manifest_path = os.path.join(app_dir, "manifest.json")
        with open(manifest_path, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh)
        sign_manifest(app_dir)
        ok, message, entry = core.install_app(manifest_path)
        self.assertTrue(ok, message)
        self.assertIsNotNone(entry)
        assert entry is not None
        ok, message, _ = core.start_app(app_id)
        self.assertTrue(ok, message)
        return entry["token"]

    def _open_stream(self, port: int, app_id: str, token: str) -> socket.socket:
        """Open a raw streaming request; returns the socket (response not read yet)."""
        body = json.dumps({"text": "hello"}).encode("utf-8")
        request = (
            f"POST /api/v1/apps/{app_id}/infer HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{port}\r\n"
            f"Authorization: Bearer {token}\r\n"
            "Accept: application/x-ndjson\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n"
            "\r\n"
        ).encode("utf-8") + body
        sock = socket.create_connection(("127.0.0.1", port), timeout=2)
        sock.sendall(request)
        return sock

    def _recv_until_chunk(self, sock: socket.socket, timeout: float = 2.0) -> bytes:
        """Read until at least one complete NDJSON chunk event arrived."""
        sock.settimeout(timeout)
        data = b""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if b'"event": "chunk"' in data and data.count(b"\n") >= 2:
                break
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                break
            if not chunk:
                break
            data += chunk
        return data

    def _recv_all(self, sock: socket.socket, timeout: float = 4.0) -> bytes:
        """Read until EOF or timeout; tolerant of a mid-line cutoff."""
        sock.settimeout(timeout)
        data = b""
        while True:
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                break
            if not chunk:
                break
            data += chunk
        return data

    def _parse_ndjson(self, raw: bytes) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for line in raw.decode("utf-8", "replace").split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # trailing partial line from a read timeout
        return events

    def _read_audit(self, core: CoreService) -> list[dict[str, Any]]:
        path = os.path.join(core.data_dir, "audit", "audit.jsonl")
        if not os.path.exists(path):
            return []
        with open(path, "r", encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def _wait_audit(
        self, core: CoreService, predicate: Any, timeout: float = 4.0
    ) -> list[dict[str, Any]]:
        deadline = time.time() + timeout
        entries = self._read_audit(core)
        while time.time() < deadline:
            entries = self._read_audit(core)
            if predicate(entries):
                return entries
            time.sleep(0.05)
        return entries

    def _wait_status(self, core: CoreService, app_id: str, status: str, timeout: float = 6.0) -> dict:
        deadline = time.time() + timeout
        health = core.health_apps()
        while time.time() < deadline:
            health = core.health_apps()
            if health.get(app_id, {}).get("status") == status:
                break
            time.sleep(0.05)
        return health

    # ---- tests ---------------------------------------------------------

    def test_app_a_stream_does_not_block_app_b_infer(self) -> None:
        core = CoreService(data_dir=os.path.join(self._tmp, "data"))
        token_a = self._add_app(core, "stream-a-app", ADAPTER_A)
        token_b = self._add_app(core, "stream-b-app", ADAPTER_B)
        harness = HttpHarness(core)
        harness.start()
        try:
            sock_a = self._open_stream(harness.port, "stream-a-app", token_a)
            try:
                first = self._recv_until_chunk(sock_a)
                self.assertIn(b'"event": "chunk"', first)

                # While A is still streaming, B normal inference must succeed quickly.
                started = time.monotonic()
                status, body = harness.infer("stream-b-app", {"text": "hi"}, token_b)
                elapsed = time.monotonic() - started
                self.assertEqual(status, 200, body)
                self.assertEqual(body["result"], {"label": "b-infer"})
                self.assertLess(elapsed, 2.0)

                # A must still be producing after B finished (not blocked/cancelled).
                more = self._recv_until_chunk(sock_a, timeout=1.5)
                self.assertIn(b'"event": "chunk"', more)

                rest = self._recv_all(sock_a, timeout=4.0)
                events = self._parse_ndjson(first + more + rest)
                self.assertEqual(events[-1]["event"], "completed", events)
                self.assertGreaterEqual(events[-1]["chunks"], 3, events)
            finally:
                sock_a.close()
        finally:
            harness.stop()

    def test_app_a_stream_does_not_block_app_b_stream(self) -> None:
        core = CoreService(data_dir=os.path.join(self._tmp, "data"))
        token_a = self._add_app(core, "stream-a-app", ADAPTER_A)
        token_b = self._add_app(core, "stream-b-app", ADAPTER_B)
        harness = HttpHarness(core)
        harness.start()
        try:
            sock_a = self._open_stream(harness.port, "stream-a-app", token_a)
            sock_b = self._open_stream(harness.port, "stream-b-app", token_b)
            try:
                first_a = self._recv_until_chunk(sock_a)
                self.assertIn(b'"event": "chunk"', first_a)

                # B's stream must run to completion while A is still streaming.
                raw_b = self._recv_all(sock_b, timeout=5.0)
                events_b = self._parse_ndjson(raw_b)
                self.assertEqual(events_b[0]["event"], "accepted", events_b)
                self.assertEqual(events_b[-1]["event"], "completed", events_b)
                self.assertGreaterEqual(events_b[-1]["chunks"], 20, events_b)

                # A is still alive and finishes with a completed event too.
                rest_a = self._recv_all(sock_a, timeout=4.0)
                events_a = self._parse_ndjson(first_a + rest_a)
                self.assertEqual(events_a[-1]["event"], "completed", events_a)
            finally:
                sock_a.close()
                sock_b.close()
        finally:
            harness.stop()

    def test_cancel_one_stream_does_not_damage_another(self) -> None:
        core = CoreService(data_dir=os.path.join(self._tmp, "data"))
        token_a = self._add_app(core, "stream-a-app", ADAPTER_A)
        token_b = self._add_app(core, "stream-b-app", ADAPTER_B)
        harness = HttpHarness(core)
        harness.start()
        try:
            sock_a = self._open_stream(harness.port, "stream-a-app", token_a)
            sock_b = self._open_stream(harness.port, "stream-b-app", token_b)
            try:
                first_a = self._recv_until_chunk(sock_a)
                self.assertIn(b'"event": "chunk"', first_a)
                first_b = self._recv_until_chunk(sock_b)
                self.assertIn(b'"event": "chunk"', first_b)

                # Cancel stream A by disconnecting; B must be unaffected.
                sock_a.close()

                self._wait_audit(
                    core,
                    lambda entries: any(
                        e.get("app_id") == "stream-a-app" and e.get("detail") == "STREAM_CANCELLED"
                        for e in entries
                    ),
                )

                raw_b = self._recv_all(sock_b, timeout=5.0)
                events_b = self._parse_ndjson(first_b + raw_b)
                self.assertEqual(events_b[-1]["event"], "completed", events_b)
                self.assertFalse(
                    any(e.get("event") in ("worker_error", "timeout") for e in events_b),
                    events_b,
                )

                # A new stream on A works after the cancellation.
                sock_a2 = self._open_stream(harness.port, "stream-a-app", token_a)
                try:
                    raw_a2 = self._recv_all(sock_a2, timeout=5.0)
                finally:
                    sock_a2.close()
                events_a2 = self._parse_ndjson(raw_a2)
                self.assertEqual(events_a2[0]["event"], "accepted", events_a2)
                self.assertEqual(events_a2[-1]["event"], "completed", events_a2)
            finally:
                sock_a.close()
                sock_b.close()
        finally:
            harness.stop()

    def test_timeout_recycles_only_affected_app(self) -> None:
        slow_limits = dict(DEFAULT_LIMITS)
        slow_limits["stream_first_chunk_timeout"] = 0.15
        slow_limits["stream_total_timeout"] = 2.0
        core = CoreService(data_dir=os.path.join(self._tmp, "data"))
        token_a = self._add_app(core, "stream-a-app", ADAPTER_A_SLOW_FIRST_CHUNK, slow_limits)
        token_b = self._add_app(core, "stream-b-app", ADAPTER_B, DEFAULT_LIMITS)
        harness = HttpHarness(core)
        harness.start()
        try:
            # A's stream times out (first chunk never arrives in 0.15s).
            sock_a = self._open_stream(harness.port, "stream-a-app", token_a)
            try:
                raw_a = self._recv_all(sock_a, timeout=4.0)
            finally:
                sock_a.close()
            events_a = self._parse_ndjson(raw_a)
            self.assertEqual(events_a[0]["event"], "accepted", events_a)
            self.assertEqual(events_a[-1]["event"], "timeout", events_a)
            self.assertEqual(events_a[-1]["reason"], "first_chunk", events_a)
            self.assertEqual(events_a[-1]["code"], "504", events_a)

            # B stays healthy through A's timeout/recycle.
            status, body = harness.infer("stream-b-app", {"text": "hi"}, token_b)
            self.assertEqual(status, 200, body)
            sock_b = self._open_stream(harness.port, "stream-b-app", token_b)
            try:
                raw_b = self._recv_all(sock_b, timeout=5.0)
            finally:
                sock_b.close()
            events_b = self._parse_ndjson(raw_b)
            self.assertEqual(events_b[-1]["event"], "completed", events_b)

            # A recovers via recycle (backoff -> ready) and serves again.
            health = self._wait_status(core, "stream-a-app", "ready")
            self.assertEqual(health["stream-a-app"]["status"], "ready", health)
            self.assertEqual(health["stream-b-app"]["status"], "ready", health)
            status, body = harness.infer("stream-a-app", {"text": "after-timeout"}, token_a)
            self.assertEqual(status, 200, body)
            self.assertEqual(body["result"], {"label": "a-infer"})

            # Only A was timed out; B has no timeout audit entry.
            entries = self._read_audit(core)
            a_timeouts = [
                e for e in entries
                if e.get("action") == "INFERENCE_TIMEOUT" and e.get("app_id") == "stream-a-app"
            ]
            b_timeouts = [
                e for e in entries
                if e.get("action") == "INFERENCE_TIMEOUT" and e.get("app_id") == "stream-b-app"
            ]
            self.assertGreaterEqual(len(a_timeouts), 1, entries)
            self.assertEqual(b_timeouts, [], entries)
        finally:
            harness.stop()

    def test_recycle_during_stream_terminates_deterministically_and_new_generation_serves(self) -> None:
        core = CoreService(data_dir=os.path.join(self._tmp, "data"))
        token_a = self._add_app(core, "stream-a-app", ADAPTER_A, DEFAULT_LIMITS)
        harness = HttpHarness(core)
        harness.start()
        try:
            sock_a = self._open_stream(harness.port, "stream-a-app", token_a)
            try:
                first = self._recv_until_chunk(sock_a)
                self.assertIn(b'"event": "chunk"', first)

                # Recycle the worker while its stream is still running.
                ok, message, _ = core.stop_app("stream-a-app")
                self.assertTrue(ok, message)
                ok, message, _ = core.start_app("stream-a-app")
                self.assertTrue(ok, message)

                # The OLD stream must terminate deterministically: no "completed"
                # event, no further chunks flowing forever; a controlled
                # worker_error "worker restarted" event closes the stream.
                rest = self._recv_all(sock_a, timeout=4.0)
                events = self._parse_ndjson(first + rest)
                self.assertFalse(
                    any(e.get("event") == "completed" for e in events), events
                )
                self.assertNotEqual(events[-1]["event"], "chunk", events)
                self.assertEqual(events[-1]["event"], "worker_error", events)
                self.assertEqual(events[-1]["message"], "worker restarted", events)
            finally:
                sock_a.close()

            # New generation serves normal inference and streaming with the
            # same public token.
            status, body = harness.infer("stream-a-app", {"text": "after-recycle"}, token_a)
            self.assertEqual(status, 200, body)
            sock_a2 = self._open_stream(harness.port, "stream-a-app", token_a)
            try:
                raw_a2 = self._recv_all(sock_a2, timeout=5.0)
            finally:
                sock_a2.close()
            events_a2 = self._parse_ndjson(raw_a2)
            self.assertEqual(events_a2[0]["event"], "accepted", events_a2)
            self.assertEqual(events_a2[-1]["event"], "completed", events_a2)
        finally:
            harness.stop()


if __name__ == "__main__":
    unittest.main()
