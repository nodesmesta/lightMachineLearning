"""Phase 2 / Day 4 — K5 minimal streaming contract RED tests.

These tests freeze the smallest approved streaming surface before production
code changes:
- public endpoint remains POST /api/v1/apps/{app_id}/infer;
- streaming is requested with Accept: application/x-ndjson;
- every event is one JSON object per NDJSON line and carries the same
  request_id/app/app_version;
- Core edge authentication remains identical;
- malicious adapter output containing newlines cannot break protocol framing.
"""
from __future__ import annotations

import hashlib
import json
import os
import pickle
import shutil
import tempfile
import unittest
import urllib.error
import urllib.request
from typing import Any

from sydeco_lightml_core.core import CoreService
from sydeco_lightml_core.secrets import write_token
from tests._signing import sign_manifest


class StreamingContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="sydeco-stream-contract-")

    def tearDown(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _install_streaming_app(self, adapter_src: str, app_id: str = "stream-app") -> tuple[CoreService, str]:
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
                "inference_timeout": 1.0,
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
        self,
        core: CoreService,
        app_id: str,
        payload: dict[str, Any],
        token: str | None,
    ) -> tuple[int, str, list[dict[str, Any]], str]:
        from sydeco_lightml_core.server import CoreHTTPServer
        import threading

        httpd = CoreHTTPServer(("127.0.0.1", 0), core)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        port = httpd.server_address[1]
        try:
            url = f"http://127.0.0.1:{port}/api/v1/apps/{app_id}/infer"
            headers = {
                "Accept": "application/x-ndjson",
                "Content-Type": "application/json",
            }
            if token is not None:
                headers["Authorization"] = "Bearer " + token
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                method="POST",
                headers=headers,
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

    def test_accept_ndjson_returns_accepted_chunk_completed_events(self) -> None:
        adapter_src = """\
class Adapter:
    def initialize(self, context):
        pass

    def infer(self, request, context):
        return {"label": "single"}

    def stream(self, request, context):
        yield {"label": "first"}
"""
        core, token = self._install_streaming_app(adapter_src)

        status, content_type, events, raw = self._stream_request(
            core, "stream-app", {"text": "hello"}, token
        )

        self.assertEqual(status, 200, raw)
        self.assertIn("application/x-ndjson", content_type)
        self.assertEqual([event["event"] for event in events], ["accepted", "chunk", "completed"])
        request_ids = {event["request_id"] for event in events}
        self.assertEqual(len(request_ids), 1)
        for event in events:
            self.assertEqual(event["app"], "stream-app")
            self.assertEqual(event["app_version"], "1.0.0")
        self.assertEqual(events[1]["seq"], 0)
        self.assertEqual(events[1]["data"], {"label": "first"})
        self.assertEqual(events[2]["chunks"], 1)

    def test_streaming_without_token_is_rejected_before_worker(self) -> None:
        adapter_src = """\
class Adapter:
    def initialize(self, context):
        pass

    def infer(self, request, context):
        return {"label": "single"}

    def stream(self, request, context):
        raise AssertionError("worker must not run without Core-edge auth")
"""
        core, _token = self._install_streaming_app(adapter_src)

        status, content_type, events, raw = self._stream_request(
            core, "stream-app", {"text": "hello"}, None
        )

        self.assertEqual(status, 401, raw)
        self.assertIn("application/json", content_type)
        self.assertEqual(events[0]["error"]["code"], "401")
        self.assertEqual(events[0]["error"]["message"], "missing or invalid token")

    def test_adapter_newlines_do_not_break_ndjson_framing(self) -> None:
        adapter_src = """\
class Adapter:
    def initialize(self, context):
        pass

    def infer(self, request, context):
        return {"label": "single"}

    def stream(self, request, context):
        yield {"label": "line1\\nline2", "note": "x\\ny"}
"""
        core, token = self._install_streaming_app(adapter_src)

        status, content_type, events, raw = self._stream_request(
            core, "stream-app", {"text": "hello"}, token
        )

        self.assertEqual(status, 200, raw)
        self.assertIn("application/x-ndjson", content_type)
        self.assertEqual(len(raw.splitlines()), 3)
        self.assertEqual(events[1]["event"], "chunk")
        self.assertEqual(events[1]["data"], {"label": "line1\nline2", "note": "x\ny"})

    def test_streaming_wrong_token_is_rejected_before_worker(self) -> None:
        adapter_src = """\
class Adapter:
    def initialize(self, context):
        pass

    def infer(self, request, context):
        return {"label": "single"}

    def stream(self, request, context):
        raise AssertionError("worker must not run with wrong Core-edge token")
"""
        core, _token = self._install_streaming_app(adapter_src)

        status, content_type, events, raw = self._stream_request(
            core, "stream-app", {"text": "hello"}, "wrong-token"
        )

        self.assertEqual(status, 401, raw)
        self.assertIn("application/json", content_type)
        self.assertEqual(events[0]["error"]["code"], "401")
        self.assertEqual(events[0]["error"]["message"], "missing or invalid token")

    def test_streaming_old_public_token_rejected_after_token_rotation(self) -> None:
        adapter_src = """\
class Adapter:
    def initialize(self, context):
        pass

    def infer(self, request, context):
        return {"label": "single"}

    def stream(self, request, context):
        yield {"label": "current-token-only"}
"""
        core, old_token = self._install_streaming_app(adapter_src)
        new_token = "n2" * 32
        write_token(core.data_dir, "stream-app", new_token)

        old_status, old_content_type, old_events, old_raw = self._stream_request(
            core, "stream-app", {"text": "hello"}, old_token
        )
        self.assertEqual(old_status, 401, old_raw)
        self.assertIn("application/json", old_content_type)
        self.assertEqual(old_events[0]["error"]["code"], "401")

        new_status, new_content_type, new_events, new_raw = self._stream_request(
            core, "stream-app", {"text": "hello"}, new_token
        )
        self.assertEqual(new_status, 200, new_raw)
        self.assertIn("application/x-ndjson", new_content_type)
        self.assertEqual([event["event"] for event in new_events], ["accepted", "chunk", "completed"])

    def test_streaming_error_response_does_not_leak_presented_token(self) -> None:
        adapter_src = """\
class Adapter:
    def initialize(self, context):
        pass

    def infer(self, request, context):
        return {"label": "single"}

    def stream(self, request, context):
        raise RuntimeError("boom should be sanitized")
"""
        core, token = self._install_streaming_app(adapter_src)

        status, content_type, events, raw = self._stream_request(
            core, "stream-app", {"text": "hello"}, token
        )

        self.assertEqual(status, 200, raw)
        self.assertIn("application/x-ndjson", content_type)
        self.assertEqual([event["event"] for event in events], ["accepted", "worker_error"])
        self.assertEqual(events[1]["message"], "internal error")
        self.assertNotIn(token, raw)

        audit_path = os.path.join(core.data_dir, "audit", "audit.jsonl")
        with open(audit_path, "r", encoding="utf-8") as fh:
            audit_blob = fh.read()
        self.assertNotIn(token, audit_blob)


if __name__ == "__main__":
    unittest.main()
