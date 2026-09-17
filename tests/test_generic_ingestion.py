"""P2 generic local file-ingestion tests."""
from __future__ import annotations

import hashlib
import http.client
import json
import os
import pickle
import shutil
import tempfile
import threading
import unittest

from sydeco_lightml_core.core import CoreService
from sydeco_lightml_core.ingress import (
    DEFAULT_INGEST_LIMIT,
    HARD_INGEST_LIMIT,
    INGEST_TTL_SECONDS,
)

from tests._http_harness import HttpHarness
from tests._signing import sign_manifest


ADAPTER = '''
class Adapter:
    def initialize(self, context):
        self.data_dir = context["data_dir"]

    def infer(self, request, context):
        file_obj = request.get("file")
        if not isinstance(file_obj, dict):
            return {"status": "ok", "source": "json", "size_bytes": len(request.get("text", ""))}
        with open(file_obj["path"], "rb") as fh:
            data = fh.read()
        return {
            "status": "ok",
            "source": "file_ref",
            "filename": file_obj.get("filename", ""),
            "content_type": file_obj.get("content_type", ""),
            "size_bytes": len(data),
        }
'''


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class GenericIngestionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="sydeco-ingest-")
        self.data_dir = os.path.join(self._tmp, "data")
        self.service = CoreService(data_dir=self.data_dir)
        self.http = HttpHarness(self.service)

    def tearDown(self) -> None:
        try:
            self.http.stop()
        except Exception:
            pass
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _bundle(
        self,
        app_id: str = "ingest-app",
        limit: int = DEFAULT_INGEST_LIMIT,
        limits_extra: dict | None = None,
    ):
        app_dir = os.path.join(self._tmp, app_id)
        os.makedirs(os.path.join(app_dir, "adapter"), exist_ok=True)
        os.makedirs(os.path.join(app_dir, "models"), exist_ok=True)
        adapter_bytes = ADAPTER.encode("utf-8")
        model_bytes = pickle.dumps({"purpose": "generic-ingestion-test"})
        with open(os.path.join(app_dir, "adapter", "main.py"), "wb") as fh:
            fh.write(adapter_bytes)
        with open(os.path.join(app_dir, "models", "model.pkl"), "wb") as fh:
            fh.write(model_bytes)
        manifest = {
            "manifest_version": 1,
            "app_id": app_id,
            "name": "Generic Ingestion Test",
            "version": "1.0.0",
            "capabilities": ["inference"],
            "models": [{
                "role": "model",
                "file": "models/model.pkl",
                "format": "pickle",
                "sha256": _sha256(model_bytes),
            }],
            "adapter": {
                "entry": "adapter/main.py",
                "files": [{"file": "adapter/main.py", "sha256": _sha256(adapter_bytes)}],
            },
            "release": {"key_id": "sydeco-test-key-v1", "signature": "manifest.sig"},
            "input_schema": {
                "type": "object",
                "required": ["operation"],
                "properties": {
                    "operation": {"type": "string"},
                    "file_ref": {"type": "string", "maxLength": 128},
                    "text": {"type": "string", "maxLength": 100000},
                },
            },
            "output_schema": {
                "type": "object",
                "required": ["status", "source", "size_bytes"],
                "properties": {
                    "status": {"type": "string"},
                    "source": {"type": "string"},
                    "filename": {"type": "string"},
                    "content_type": {"type": "string"},
                    "size_bytes": {"type": "integer"},
                },
            },
            "permissions": {"network": "none"},
            "api": {"authentication": "token"},
            "resource_limits": {
                "max_memory": 134217728,
                "max_cpu": 50,
                "inference_timeout": 30,
                "concurrency": 1,
            },
            "limits": {"max_ingest_bytes": limit},
            "dependencies": [],
        }
        if limits_extra:
            manifest["limits"].update(limits_extra)
        with open(os.path.join(app_dir, "manifest.json"), "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=2, sort_keys=True)
        sign_manifest(app_dir)
        ok, message, entry = self.service.install_app(os.path.join(app_dir, "manifest.json"), app_dir)
        self.assertTrue(ok, message)
        self.assertIsNotNone(entry)
        assert entry is not None
        ok, message, _ = self.service.start_app(app_id)
        self.assertTrue(ok, message)
        return app_dir, entry["token"]

    def _start_once(self) -> None:
        if self.http.httpd is None:
            self.http.start()

    def _stream_request(self, app_id: str, payload: dict, token: str) -> tuple[int, str]:
        conn = http.client.HTTPConnection("127.0.0.1", self.http.port, timeout=10)
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/x-ndjson",
        }
        conn.request(
            "POST", f"/api/v1/apps/{app_id}/infer",
            body=json.dumps(payload).encode("utf-8"), headers=headers,
        )
        resp = conn.getresponse()
        lines = []
        for _ in range(2):
            line = resp.readline()
            if not line:
                break
            lines.append(line.decode("utf-8"))
        body = "".join(lines)
        status = resp.status
        conn.close()
        return status, body

    def test_upload_and_process_multi_megabyte_file_ref(self) -> None:
        _app_dir, token = self._bundle(limit=DEFAULT_INGEST_LIMIT)
        self._start_once()
        for size in (256 * 1024, 700 * 1024, 1024 * 1024, 2 * 1024 * 1024, DEFAULT_INGEST_LIMIT):
            payload = (b"A" * size)
            status, body = self.http.ingest(
                "ingest-app", payload, token, filename=f"sample-{size}.txt", content_type="text/plain"
            )
            self.assertEqual(status, 200, body)
            upload = body["upload"]
            self.assertEqual(upload["size_bytes"], size)
            self.assertNotIn("path", upload)
            status, body = self.http.infer(
                "ingest-app", {"operation": "process", "file_ref": upload["file_ref"]}, token
            )
            self.assertEqual(status, 200, body)
            self.assertEqual(body["result"]["source"], "file_ref")
            self.assertEqual(body["result"]["size_bytes"], size)
            self.assertEqual(body["result"]["content_type"], "text/plain")
            # References are one-shot and cleaned after consumption.
            status, body = self.http.infer(
                "ingest-app", {"operation": "process", "file_ref": upload["file_ref"]}, token
            )
            self.assertEqual(status, 404, body)

    def test_existing_json_inference_still_works(self) -> None:
        _app_dir, token = self._bundle()
        self._start_once()
        status, body = self.http.infer(
            "ingest-app", {"operation": "process", "text": "hello"}, token
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(body["result"]["source"], "json")
        self.assertEqual(body["result"]["size_bytes"], 5)

    def test_zero_byte_and_oversized_uploads_are_rejected(self) -> None:
        _app_dir, token = self._bundle(limit=1024)
        self._start_once()
        status, body = self.http.ingest("ingest-app", b"", token)
        self.assertEqual(status, 400, body)
        self.assertIn("empty upload", body["error"]["message"])
        status, body = self.http.request_declared(
            "POST", "/api/v1/apps/ingest-app/ingest", 1025, token=token
        )
        self.assertEqual(status, 413, body)
        self.assertIn("file exceeds upload size limit", body["error"]["message"])

    def test_invalid_cross_app_and_expired_references_fail_closed(self) -> None:
        _a_dir, token_a = self._bundle(app_id="ingest-a")
        _b_dir, token_b = self._bundle(app_id="ingest-b")
        self._start_once()
        status, body = self.http.ingest("ingest-a", b"abc", token_a)
        self.assertEqual(status, 200, body)
        file_ref = body["upload"]["file_ref"]
        status, body = self.http.infer(
            "ingest-b", {"operation": "process", "file_ref": file_ref}, token_b
        )
        self.assertEqual(status, 404, body)
        status, body = self.http.infer(
            "ingest-a", {"operation": "process", "file_ref": "../escape"}, token_a
        )
        self.assertEqual(status, 400, body)
        self.service.ingress.consume("ingest-a", file_ref)
        status, body = self.http.infer(
            "ingest-a", {"operation": "process", "file_ref": file_ref}, token_a
        )
        self.assertEqual(status, 404, body)

    def test_file_ref_consumed_after_readiness_failure(self) -> None:
        _app_dir, token = self._bundle()
        self._start_once()
        status, body = self.http.ingest("ingest-app", b"readiness", token)
        self.assertEqual(status, 200, body)
        file_ref = body["upload"]["file_ref"]
        ok, _message, _ = self.service.stop_app("ingest-app")
        self.assertTrue(ok)
        status, body = self.http.infer(
            "ingest-app", {"operation": "process", "file_ref": file_ref}, token
        )
        self.assertEqual(status, 503, body)
        ok, message, _ = self.service.start_app("ingest-app")
        self.assertTrue(ok, message)
        status, body = self.http.infer(
            "ingest-app", {"operation": "process", "file_ref": file_ref}, token
        )
        self.assertEqual(status, 404, body)

    def test_file_ref_consumed_after_streaming_error(self) -> None:
        _app_dir, token = self._bundle()
        self._start_once()
        status, body = self.http.ingest("ingest-app", b"streaming", token)
        self.assertEqual(status, 200, body)
        file_ref = body["upload"]["file_ref"]
        status, body = self._stream_request(
            "ingest-app", {"operation": "process", "file_ref": file_ref}, token
        )
        self.assertEqual(status, 200, body)
        self.assertIn('"event": "worker_error"', body)
        self.assertIn('"message": "streaming not supported"', body)
        status, body = self.http.infer(
            "ingest-app", {"operation": "process", "file_ref": file_ref}, token
        )
        self.assertEqual(status, 404, body)

    def test_file_ref_concurrent_reuse_allows_only_one_consumer(self) -> None:
        _app_dir, token = self._bundle()
        self._start_once()
        status, body = self.http.ingest("ingest-app", b"concurrent", token)
        self.assertEqual(status, 200, body)
        file_ref = body["upload"]["file_ref"]
        barrier = threading.Barrier(3)
        results = []

        def consume_once() -> None:
            barrier.wait(timeout=10)
            results.append(self.http.infer(
                "ingest-app", {"operation": "process", "file_ref": file_ref}, token
            ))

        threads = [threading.Thread(target=consume_once) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait(timeout=10)
        for thread in threads:
            thread.join(timeout=10)
        self.assertEqual(len(results), 2)
        statuses = sorted(status for status, _body in results)
        self.assertEqual(statuses, [200, 404], results)
        status, body = self.http.infer(
            "ingest-app", {"operation": "process", "file_ref": file_ref}, token
        )
        self.assertEqual(status, 404, body)

    def test_core_hard_cap_overrides_manifest_limit(self) -> None:
        _app_dir, token = self._bundle(limit=HARD_INGEST_LIMIT * 2)
        self._start_once()
        status, body = self.http.request_declared(
            "POST", "/api/v1/apps/ingest-app/ingest", HARD_INGEST_LIMIT + 1, token=token
        )
        self.assertEqual(status, 413, body)

    def test_pending_reference_count_quota_and_release_after_consume(self) -> None:
        _app_dir, token = self._bundle(
            limits_extra={"max_pending_refs_per_app": 2, "max_pending_ingest_bytes_per_app": 1024}
        )
        self._start_once()
        status, first = self.http.ingest("ingest-app", b"a", token)
        self.assertEqual(status, 200, first)
        status, second = self.http.ingest("ingest-app", b"b", token)
        self.assertEqual(status, 200, second)
        status, body = self.http.ingest("ingest-app", b"c", token)
        self.assertEqual(status, 429, body)
        self.assertIn("pending file reference limit exceeded", body["error"]["message"])

        status, body = self.http.infer(
            "ingest-app", {"operation": "process", "file_ref": first["upload"]["file_ref"]}, token
        )
        self.assertEqual(status, 200, body)
        status, body = self.http.ingest("ingest-app", b"c", token)
        self.assertEqual(status, 200, body)

    def test_pending_reference_count_quota_is_atomic_for_concurrent_uploads(self) -> None:
        _app_dir, token = self._bundle(
            limits_extra={"max_pending_refs_per_app": 1, "max_pending_ingest_bytes_per_app": 1024}
        )
        self._start_once()
        barrier = threading.Barrier(3)
        results = []
        lock = threading.Lock()

        def upload_once(payload: bytes) -> None:
            barrier.wait(timeout=10)
            result = self.http.ingest("ingest-app", payload, token)
            with lock:
                results.append(result)

        threads = [
            threading.Thread(target=upload_once, args=(b"concurrent-a",)),
            threading.Thread(target=upload_once, args=(b"concurrent-b",)),
        ]
        for thread in threads:
            thread.start()
        barrier.wait(timeout=10)
        for thread in threads:
            thread.join(timeout=10)

        self.assertEqual(len(results), 2)
        statuses = sorted(status for status, _body in results)
        self.assertEqual(statuses, [200, 429], results)
        usage = self.service.ingress.pending_usage("ingest-app")
        self.assertEqual(usage["pending_refs"], 1)
        self.assertIn(usage["pending_bytes"], {len(b"concurrent-a"), len(b"concurrent-b")})

    def test_pending_byte_quota_rejects_without_partial_file(self) -> None:
        _app_dir, token = self._bundle(
            limits_extra={"max_pending_refs_per_app": 10, "max_pending_ingest_bytes_per_app": 10}
        )
        self._start_once()
        status, body = self.http.ingest("ingest-app", b"123456", token)
        self.assertEqual(status, 200, body)
        usage = self.service.ingress.pending_usage("ingest-app")
        self.assertEqual(usage["pending_refs"], 1)
        self.assertEqual(usage["pending_bytes"], 6)

        status, body = self.http.ingest("ingest-app", b"12345", token)
        self.assertEqual(status, 413, body)
        self.assertIn("pending ingress byte limit exceeded", body["error"]["message"])
        usage = self.service.ingress.pending_usage("ingest-app")
        self.assertEqual(usage["pending_refs"], 1)
        self.assertEqual(usage["pending_bytes"], 6)

    def test_pending_quota_released_after_expiration(self) -> None:
        _app_dir, token = self._bundle(
            limits_extra={"max_pending_refs_per_app": 1, "max_pending_ingest_bytes_per_app": 1024}
        )
        self._start_once()
        status, body = self.http.ingest("ingest-app", b"expire", token)
        self.assertEqual(status, 200, body)
        status, body = self.http.ingest("ingest-app", b"blocked", token)
        self.assertEqual(status, 429, body)

        self.service.ingress.cleanup_expired(
            "ingest-app", now=__import__("time").time() + INGEST_TTL_SECONDS + 1
        )
        status, body = self.http.ingest("ingest-app", b"after-expire", token)
        self.assertEqual(status, 200, body)

    def test_pending_quota_released_after_failed_processing(self) -> None:
        _app_dir, token = self._bundle(
            limits_extra={"max_pending_refs_per_app": 1, "max_pending_ingest_bytes_per_app": 1024}
        )
        self._start_once()
        status, upload = self.http.ingest("ingest-app", b"failure", token)
        self.assertEqual(status, 200, upload)
        ok, _message, _ = self.service.stop_app("ingest-app")
        self.assertTrue(ok)
        status, body = self.http.infer(
            "ingest-app", {"operation": "process", "file_ref": upload["upload"]["file_ref"]}, token
        )
        self.assertEqual(status, 503, body)
        status, body = self.http.ingest("ingest-app", b"after-failure", token)
        self.assertEqual(status, 200, body)

    def test_pending_quota_is_app_scoped(self) -> None:
        _a_dir, token_a = self._bundle(
            app_id="quota-a",
            limits_extra={"max_pending_refs_per_app": 1, "max_pending_ingest_bytes_per_app": 1024},
        )
        _b_dir, token_b = self._bundle(
            app_id="quota-b",
            limits_extra={"max_pending_refs_per_app": 1, "max_pending_ingest_bytes_per_app": 1024},
        )
        self._start_once()
        status, body = self.http.ingest("quota-a", b"a", token_a)
        self.assertEqual(status, 200, body)
        status, body = self.http.ingest("quota-a", b"a2", token_a)
        self.assertEqual(status, 429, body)
        status, body = self.http.ingest("quota-b", b"b", token_b)
        self.assertEqual(status, 200, body)


if __name__ == "__main__":
    unittest.main()
