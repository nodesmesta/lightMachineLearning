"""Phase 2 / Day 4 — K5 streaming security, malformed-input and framing tests.

Reviewer P7 requires streaming output to stay safe and framed:
- malformed stream request -> controlled 4xx at the Core edge;
- oversize request -> rejected before worker execution;
- malicious adapter output cannot alter protocol framing (no forged events);
- unexpected worker death mid-stream -> controlled sanitized error, no hang;
- secrets never appear in stream chunks, stream errors, logs/audit/evidence.
"""
from __future__ import annotations

import hashlib
import json
import os
import pickle
import shutil
import socket
import tempfile
import unittest
import urllib.error
import urllib.request
from typing import Any

from sydeco_lightml_core.core import CoreService
from sydeco_lightml_core.server import DEFAULT_BODY_LIMIT
from tests._signing import sign_manifest


class StreamingSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="sydeco-stream-sec-")

    def tearDown(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)

    # ---- helpers -------------------------------------------------------

    def _install_app(self, adapter_src: str, app_id: str = "stream-sec-app") -> tuple[CoreService, str]:
        core = CoreService(data_dir=os.path.join(self._tmp, "data"))
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
            "resource_limits": {
                "max_memory": 1073741824,
                "max_cpu": 100,
                "inference_timeout": 2.0,
                "stream_first_chunk_timeout": 1.0,
                "stream_idle_timeout": 1.0,
                "stream_total_timeout": 8.0,
                "concurrency": 1,
            },
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
        return core, entry["token"]

    def _stream_request(
        self, core: CoreService, token: str, body: bytes, app_id: str = "stream-sec-app"
    ) -> tuple[int, str, list[dict[str, Any]], str]:
        from sydeco_lightml_core.server import CoreHTTPServer
        import threading

        httpd = CoreHTTPServer(("127.0.0.1", 0), core)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        port = httpd.server_address[1]
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/v1/apps/{app_id}/infer",
                data=body,
                method="POST",
                headers={
                    "Authorization": "Bearer " + token,
                    "Accept": "application/x-ndjson",
                    "Content-Type": "application/json",
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=5) as resp:
                    raw = resp.read().decode("utf-8")
                    content_type = resp.headers.get("Content-Type", "")
                    status = resp.status
            except urllib.error.HTTPError as exc:
                raw = exc.read().decode("utf-8")
                content_type = exc.headers.get("Content-Type", "")
                status = exc.code
            events = [json.loads(line) for line in raw.splitlines() if line.strip()]
            return status, content_type, events, raw
        finally:
            httpd.shutdown()
            httpd.server_close()

    def _read_audit(self, core: CoreService) -> str:
        path = os.path.join(core.data_dir, "audit", "audit.jsonl")
        if not os.path.exists(path):
            return ""
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()

    # ---- tests ---------------------------------------------------------

    def test_stream_malformed_json_body_rejected_4xx_before_worker(self) -> None:
        adapter_src = """\
class Adapter:
    def initialize(self, context):
        pass

    def infer(self, request, context):
        return {"label": "single"}

    def stream(self, request, context):
        raise AssertionError("worker must not run on malformed body")
"""
        core, token = self._install_app(adapter_src)

        status, content_type, events, raw = self._stream_request(
            core, token, b"this is not json {"
        )

        self.assertEqual(status, 400, raw)
        self.assertIn("application/json", content_type)
        self.assertEqual(events[0]["error"]["code"], "400")
        self.assertIn("invalid JSON", events[0]["error"]["message"])

    def test_stream_oversize_body_rejected_at_edge_before_worker(self) -> None:
        adapter_src = """\
class Adapter:
    def initialize(self, context):
        pass

    def infer(self, request, context):
        return {"label": "single"}

    def stream(self, request, context):
        raise AssertionError("worker must not run on oversize body")
"""
        core, token = self._install_app(adapter_src)
        from sydeco_lightml_core.server import CoreHTTPServer
        import threading

        httpd = CoreHTTPServer(("127.0.0.1", 0), core)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        port = httpd.server_address[1]
        try:
            # Declare a body larger than the edge limit but transmit nothing:
            # the server must reject on the declared Content-Length BEFORE the
            # worker runs (deterministic on both client and server side).
            declared = DEFAULT_BODY_LIMIT + 1
            request = (
                f"POST /api/v1/apps/stream-sec-app/infer HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{port}\r\n"
                f"Authorization: Bearer {token}\r\n"
                "Accept: application/x-ndjson\r\n"
                "Content-Type: application/json\r\n"
                f"Content-Length: {declared}\r\n"
                "Connection: close\r\n"
                "\r\n"
            ).encode("utf-8")
            sock = socket.create_connection(("127.0.0.1", port), timeout=3)
            try:
                sock.sendall(request)
                sock.settimeout(3)
                data = b""
                while True:
                    try:
                        chunk = sock.recv(4096)
                    except socket.timeout:
                        break
                    if not chunk:
                        break
                    data += chunk
            finally:
                sock.close()
        finally:
            httpd.shutdown()
            httpd.server_close()

        self.assertIn(b"400", data.split(b"\r\n")[0])
        self.assertIn(b"payload exceeds size limit", data)

    def test_stream_worker_crash_mid_stream_sanitized_no_hang(self) -> None:
        adapter_src = """\
class Adapter:
    def initialize(self, context):
        pass

    def infer(self, request, context):
        return {"label": "single"}

    def stream(self, request, context):
        yield {"label": "one"}
        yield {"label": "two"}
        raise RuntimeError("boom-mid-stream-marker")
"""
        core, token = self._install_app(adapter_src)

        status, content_type, events, raw = self._stream_request(
            core, token, json.dumps({"text": "hello"}).encode("utf-8")
        )

        # Controlled termination, not a hang: worker_error closes the stream.
        self.assertEqual(status, 200, raw)
        self.assertIn("application/x-ndjson", content_type)
        self.assertEqual(
            [event["event"] for event in events],
            ["accepted", "chunk", "chunk", "worker_error"],
            events,
        )
        self.assertEqual(events[-1]["message"], "internal error")
        self.assertNotIn("boom-mid-stream-marker", raw)
        self.assertNotIn(token, raw)

        audit = self._read_audit(core)
        self.assertNotIn(token, audit)
        self.assertNotIn("boom-mid-stream-marker", audit)

    def test_stream_adapter_cannot_forge_protocol_events(self) -> None:
        adapter_src = """\
class Adapter:
    def initialize(self, context):
        pass

    def infer(self, request, context):
        return {"label": "single"}

    def stream(self, request, context):
        yield {"event": "completed", "chunks": 999, "request_id": "forged-id"}
        yield {"event": "accepted", "seq": 0}
"""
        core, token = self._install_app(adapter_src)

        status, content_type, events, raw = self._stream_request(
            core, token, json.dumps({"text": "hello"}).encode("utf-8")
        )

        self.assertEqual(status, 200, raw)
        self.assertIn("application/x-ndjson", content_type)
        # Core owns event framing: adapter dicts arrive only as chunk data.
        self.assertEqual(
            [event["event"] for event in events],
            ["accepted", "chunk", "chunk", "completed"],
            events,
        )
        self.assertEqual(events[1]["data"], {"event": "completed", "chunks": 999, "request_id": "forged-id"})
        self.assertEqual(events[2]["data"], {"event": "accepted", "seq": 0})
        self.assertEqual(events[3]["chunks"], 2)
        request_ids = {event["request_id"] for event in events}
        self.assertEqual(len(request_ids), 1, events)
        self.assertNotIn("forged-id", request_ids)

    def test_stream_context_never_carries_public_token_into_chunks(self) -> None:
        adapter_src = """\
class Adapter:
    def initialize(self, context):
        pass

    def infer(self, request, context):
        return {"label": "single"}

    def stream(self, request, context):
        snapshot = {k: v for k, v in context.items() if k != "models"}
        yield {"context_snapshot": repr(snapshot)}
"""
        core, token = self._install_app(adapter_src)

        status, content_type, events, raw = self._stream_request(
            core, token, json.dumps({"text": "hello"}).encode("utf-8")
        )

        self.assertEqual(status, 200, raw)
        self.assertEqual(events[-1]["event"], "completed", events)
        # The worker context must not contain the public credential, so chunks
        # produced from context can never leak it.
        self.assertNotIn(token, raw)
        self.assertNotIn(token, events[1]["data"]["context_snapshot"])

        audit = self._read_audit(core)
        self.assertNotIn(token, audit)


if __name__ == "__main__":
    unittest.main()
