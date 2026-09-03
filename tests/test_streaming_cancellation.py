"""Phase 2 / Day 4 — K5 streaming client-cancellation RED tests.

Reviewer P4 requires client disconnect to be explicit and bounded:
- Core records a STREAM_CANCELLED audit event;
- cancellation does not recycle a healthy worker unnecessarily;
- later ordinary inference still works;
- later streams are not permanently blocked by abandoned producer work.
"""
from __future__ import annotations

import hashlib
import json
import os
import pickle
import shutil
import socket
import tempfile
import threading
import time
import unittest
from typing import Any

from sydeco_lightml_core.core import CoreService
from tests._http_harness import HttpHarness
from tests._signing import sign_manifest


class StreamingCancellationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="sydeco-stream-cancel-")

    def tearDown(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _install_app(self) -> tuple[CoreService, str]:
        core = CoreService(data_dir=os.path.join(self._tmp, "data"))
        app_dir = os.path.join(self._tmp, "stream-cancel-app")
        os.makedirs(os.path.join(app_dir, "models"), exist_ok=True)
        os.makedirs(os.path.join(app_dir, "adapter"), exist_ok=True)
        model_bytes = pickle.dumps({"m": 1})
        adapter_src = """\
import time

ACTIVE = False

class Adapter:
    def initialize(self, context):
        pass

    def infer(self, request, context):
        return {"label": "normal"}

    def stream(self, request, context):
        global ACTIVE
        if ACTIVE:
            raise RuntimeError("previous stream still active")
        ACTIVE = True
        try:
            for i in range(50):
                yield {"label": str(i)}
                time.sleep(0.02)
        finally:
            ACTIVE = False
"""
        adapter_path = os.path.join(app_dir, "adapter", "main.py")
        with open(os.path.join(app_dir, "models", "model.pkl"), "wb") as fh:
            fh.write(model_bytes)
        with open(adapter_path, "w", encoding="utf-8") as fh:
            fh.write(adapter_src)
        manifest = {
            "manifest_version": 1,
            "app_id": "stream-cancel-app",
            "name": "stream-cancel-app",
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
                "stream_total_timeout": 3.0,
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
        ok, message, _ = core.start_app("stream-cancel-app")
        self.assertTrue(ok, message)
        return core, entry["token"]

    def _read_until_first_chunk_then_close(self, port: int, token: str) -> None:
        body = json.dumps({"text": "hello"}).encode("utf-8")
        request = (
            "POST /api/v1/apps/stream-cancel-app/infer HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{port}\r\n"
            f"Authorization: Bearer {token}\r\n"
            "Accept: application/x-ndjson\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n"
            "\r\n"
        ).encode("utf-8") + body
        sock = socket.create_connection(("127.0.0.1", port), timeout=2)
        try:
            sock.sendall(request)
            sock.settimeout(2)
            data = b""
            deadline = time.time() + 2
            while b'"event": "chunk"' not in data and time.time() < deadline:
                data += sock.recv(4096)
            self.assertIn(b'"event": "chunk"', data)
        finally:
            sock.close()

    def _read_audit(self, core: CoreService) -> list[dict[str, Any]]:
        path = os.path.join(core.data_dir, "audit", "audit.jsonl")
        if not os.path.exists(path):
            return []
        with open(path, "r", encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def _wait_for_audit_detail(self, core: CoreService, detail: str) -> list[dict[str, Any]]:
        return self._wait_for_audit_detail_count(core, detail, 1)

    def _wait_for_audit_detail_count(self, core: CoreService, detail: str, count: int) -> list[dict[str, Any]]:
        deadline = time.time() + 2
        while time.time() < deadline:
            entries = self._read_audit(core)
            if len([entry for entry in entries if entry.get("detail") == detail]) >= count:
                return entries
            time.sleep(0.05)
        return self._read_audit(core)

    def test_client_disconnect_records_cancelled_and_next_infer_works(self) -> None:
        core, token = self._install_app()
        harness = HttpHarness(core)
        harness.start()
        try:
            self._read_until_first_chunk_then_close(harness.port, token)
            entries = self._wait_for_audit_detail(core, "STREAM_CANCELLED")
            self.assertTrue(
                any(entry.get("detail") == "STREAM_CANCELLED" and entry.get("http_status") == 499 for entry in entries),
                entries,
            )
            self.assertFalse(
                any(entry.get("action") == "WORKER_RESTART" for entry in entries),
                entries,
            )
            status, payload = harness.infer("stream-cancel-app", {"text": "after-cancel"}, token)
            self.assertEqual(status, 200, payload)
            self.assertEqual(payload["result"], {"label": "normal"})
        finally:
            harness.stop()

    def test_client_disconnect_does_not_permanently_block_next_stream(self) -> None:
        core, token = self._install_app()
        harness = HttpHarness(core)
        harness.start()
        try:
            self._read_until_first_chunk_then_close(harness.port, token)
            entries = self._wait_for_audit_detail(core, "STREAM_CANCELLED")
            self.assertTrue(any(entry.get("detail") == "STREAM_CANCELLED" for entry in entries), entries)

            self._read_until_first_chunk_then_close(harness.port, token)
            entries = self._wait_for_audit_detail_count(core, "STREAM_CANCELLED", 2)
            cancelled = [entry for entry in entries if entry.get("detail") == "STREAM_CANCELLED"]
            self.assertGreaterEqual(len(cancelled), 2, entries)
        finally:
            harness.stop()


if __name__ == "__main__":
    unittest.main()
