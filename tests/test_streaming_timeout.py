"""Phase 2 / Day 4 — K5 streaming timeout containment RED tests.

Reviewer P3 requires three distinct streaming bounds:
- time to first chunk;
- idle time between chunks;
- maximum total stream duration.

These tests intentionally pin the public Core behavior first. The stream must
terminate deterministically with a sanitized NDJSON timeout event instead of
hanging or completing after violating the configured bound.
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
import unittest
import urllib.error
import urllib.request
from typing import Any

from sydeco_lightml_core.core import CoreService
from tests._signing import sign_manifest


class StreamingTimeoutTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="sydeco-stream-timeout-")

    def tearDown(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _install_app(self, adapter_src: str, resource_limits: dict[str, Any]) -> tuple[CoreService, str]:
        core = CoreService(data_dir=os.path.join(self._tmp, "data"))
        app_dir = os.path.join(self._tmp, "stream-timeout-app")
        os.makedirs(os.path.join(app_dir, "models"), exist_ok=True)
        os.makedirs(os.path.join(app_dir, "adapter"), exist_ok=True)
        model_bytes = pickle.dumps({"m": 1})
        adapter_path = os.path.join(app_dir, "adapter", "main.py")
        with open(os.path.join(app_dir, "models", "model.pkl"), "wb") as fh:
            fh.write(model_bytes)
        with open(adapter_path, "w", encoding="utf-8") as fh:
            fh.write(adapter_src)
        manifest = {
            "manifest_version": 1,
            "app_id": "stream-timeout-app",
            "name": "stream-timeout-app",
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
            "resource_limits": resource_limits,
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
        ok, message, _ = core.start_app("stream-timeout-app")
        self.assertTrue(ok, message)
        return core, entry["token"]

    def _stream_request(self, core: CoreService, token: str) -> tuple[int, list[dict[str, Any]], str]:
        from sydeco_lightml_core.server import CoreHTTPServer

        httpd = CoreHTTPServer(("127.0.0.1", 0), core)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        port = httpd.server_address[1]
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/v1/apps/stream-timeout-app/infer",
                data=json.dumps({"text": "hello"}).encode("utf-8"),
                method="POST",
                headers={
                    "Authorization": "Bearer " + token,
                    "Accept": "application/x-ndjson",
                    "Content-Type": "application/json",
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=3) as resp:
                    raw = resp.read().decode("utf-8")
                    status = resp.status
            except urllib.error.HTTPError as exc:
                raw = exc.read().decode("utf-8")
                status = exc.code
            events = [json.loads(line) for line in raw.splitlines() if line.strip()]
            return status, events, raw
        finally:
            httpd.shutdown()
            httpd.server_close()

    def _assert_timeout_event(self, events: list[dict[str, Any]], reason: str) -> None:
        self.assertGreaterEqual(len(events), 2, events)
        self.assertEqual(events[0]["event"], "accepted")
        self.assertEqual(events[-1]["event"], "timeout")
        self.assertEqual(events[-1]["reason"], reason)
        self.assertEqual(events[-1]["code"], "504")
        request_ids = {event["request_id"] for event in events}
        self.assertEqual(len(request_ids), 1)

    def test_first_chunk_timeout_terminates_stream(self) -> None:
        adapter_src = """\
import time

class Adapter:
    def initialize(self, context):
        pass

    def infer(self, request, context):
        return {"label": "normal"}

    def stream(self, request, context):
        time.sleep(0.35)
        yield {"label": "too-late"}
"""
        core, token = self._install_app(adapter_src, {
            "max_memory": 1073741824,
            "max_cpu": 100,
            "inference_timeout": 2.0,
            "stream_first_chunk_timeout": 0.1,
            "stream_idle_timeout": 1.0,
            "stream_total_timeout": 1.0,
            "concurrency": 1,
        })

        status, events, raw = self._stream_request(core, token)

        self.assertEqual(status, 200, raw)
        self._assert_timeout_event(events, "first_chunk")
        self.assertNotIn("too-late", raw)

    def test_idle_timeout_after_first_chunk_terminates_stream(self) -> None:
        adapter_src = """\
import time

class Adapter:
    def initialize(self, context):
        pass

    def infer(self, request, context):
        return {"label": "normal"}

    def stream(self, request, context):
        yield {"label": "first"}
        time.sleep(0.35)
        yield {"label": "too-late"}
"""
        core, token = self._install_app(adapter_src, {
            "max_memory": 1073741824,
            "max_cpu": 100,
            "inference_timeout": 2.0,
            "stream_first_chunk_timeout": 1.0,
            "stream_idle_timeout": 0.1,
            "stream_total_timeout": 1.0,
            "concurrency": 1,
        })

        status, events, raw = self._stream_request(core, token)

        self.assertEqual(status, 200, raw)
        self.assertEqual(events[1]["event"], "chunk")
        self.assertEqual(events[1]["data"], {"label": "first"})
        self._assert_timeout_event(events, "idle")
        self.assertNotIn("too-late", raw)

    def test_total_timeout_bounds_long_stream(self) -> None:
        adapter_src = """\
import time

class Adapter:
    def initialize(self, context):
        pass

    def infer(self, request, context):
        return {"label": "normal"}

    def stream(self, request, context):
        for i in range(10):
            yield {"label": str(i)}
            time.sleep(0.06)
"""
        core, token = self._install_app(adapter_src, {
            "max_memory": 1073741824,
            "max_cpu": 100,
            "inference_timeout": 2.0,
            "stream_first_chunk_timeout": 1.0,
            "stream_idle_timeout": 1.0,
            "stream_total_timeout": 0.18,
            "concurrency": 1,
        })

        status, events, raw = self._stream_request(core, token)

        self.assertEqual(status, 200, raw)
        self.assertTrue(any(event.get("event") == "chunk" for event in events), events)
        self._assert_timeout_event(events, "total")
        self.assertLess(len([event for event in events if event.get("event") == "chunk"]), 10)


if __name__ == "__main__":
    unittest.main()
