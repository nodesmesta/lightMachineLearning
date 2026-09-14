"""P1/P2 network permission enforcement tests."""
from __future__ import annotations

import hashlib
import json
import os
import pickle
import shutil
import socket
import tempfile
import threading
import unittest

from sydeco_lightml_core.core import CoreService
from sydeco_lightml_core.validation import validate_manifest

from tests._http_harness import HttpHarness
from tests._signing import sign_manifest


ADAPTER = '''
import socket

class Adapter:
    def initialize(self, context):
        pass

    def infer(self, request, context):
        operation = request.get("operation", "create_connection")
        host = request.get("host", "example.com")
        port = request.get("port", 80)
        result = {"operation": operation, "attempted": True}
        try:
            if operation == "create_connection":
                with socket.create_connection((host, port), timeout=2.0):
                    pass
            elif operation == "direct_socket":
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                try:
                    sock.settimeout(2.0)
                    sock.connect((host, port))
                finally:
                    sock.close()
            elif operation == "dns":
                socket.getaddrinfo(host, port)
            else:
                raise ValueError("unknown operation")
            result.update({"allowed": True, "error": ""})
        except Exception as exc:
            result.update({"allowed": False, "error": type(exc).__name__ + ": " + str(exc)})
        return {"status": "ok", "network": result}
'''


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class NetworkPermissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="sydeco-network-perm-")
        self.data_dir = os.path.join(self._tmp, "data")

    def tearDown(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _bundle(self, network="none", include_permissions=True):
        app_dir = os.path.join(self._tmp, "app")
        os.makedirs(os.path.join(app_dir, "adapter"), exist_ok=True)
        os.makedirs(os.path.join(app_dir, "models"), exist_ok=True)
        adapter_bytes = ADAPTER.encode("utf-8")
        model_bytes = pickle.dumps({"purpose": "network-permission-test"})
        with open(os.path.join(app_dir, "adapter", "main.py"), "wb") as fh:
            fh.write(adapter_bytes)
        with open(os.path.join(app_dir, "models", "model.pkl"), "wb") as fh:
            fh.write(model_bytes)
        manifest = {
            "manifest_version": 1,
            "app_id": "network-test",
            "name": "Network Permission Test",
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
                "properties": {"operation": {"type": "string"}},
            },
            "output_schema": {
                "type": "object",
                "required": ["status", "network"],
                "properties": {
                    "status": {"type": "string"},
                    "network": {"type": "object"},
                },
            },
            "api": {"authentication": "token"},
            "resource_limits": {
                "max_memory": 134217728,
                "max_cpu": 50,
                "inference_timeout": 10,
                "concurrency": 1,
            },
            "dependencies": [],
        }
        if include_permissions:
            manifest["permissions"] = {"network": network}
        manifest_path = os.path.join(app_dir, "manifest.json")
        with open(manifest_path, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=2, sort_keys=True)
        sign_manifest(app_dir)
        return app_dir, manifest

    def test_missing_network_permission_fails_closed(self) -> None:
        app_dir, manifest = self._bundle(include_permissions=False)
        ok, errors = validate_manifest(manifest)
        self.assertFalse(ok)
        self.assertIn("permissions missing", "; ".join(errors))
        service = CoreService(data_dir=self.data_dir)
        ok, message, _entry = service.install_app(os.path.join(app_dir, "manifest.json"), app_dir)
        self.assertFalse(ok)
        self.assertIn("permissions missing", message)

    def test_invalid_network_permission_rejected(self) -> None:
        app_dir, manifest = self._bundle(network="invalid-profile")
        ok, errors = validate_manifest(manifest)
        self.assertFalse(ok)
        self.assertIn("permissions.network", "; ".join(errors))
        service = CoreService(data_dir=self.data_dir)
        ok, message, _entry = service.install_app(os.path.join(app_dir, "manifest.json"), app_dir)
        self.assertFalse(ok)
        self.assertIn("permissions.network", message)

    def test_network_none_denies_socket_and_dns(self) -> None:
        app_dir, _manifest = self._bundle(network="none")
        service = CoreService(data_dir=self.data_dir)
        ok, message, entry = service.install_app(os.path.join(app_dir, "manifest.json"), app_dir)
        self.assertTrue(ok, message)
        self.assertIsNotNone(entry)
        ok, message, _ = service.start_app("network-test")
        self.assertTrue(ok, message)
        http = HttpHarness(service)
        http.start()
        try:
            for operation in ("create_connection", "direct_socket", "dns"):
                status, body = http.infer("network-test", {"operation": operation}, entry["token"])
                self.assertEqual(status, 200, body)
                self.assertFalse(body["result"]["network"]["allowed"], body)
                self.assertIn("NetworkPermissionDenied", body["result"]["network"]["error"])
        finally:
            http.stop()

    def test_explicit_outbound_profile_is_controlled_allow(self) -> None:
        app_dir, manifest = self._bundle(network="outbound")
        ok, errors = validate_manifest(manifest)
        self.assertTrue(ok, errors)

        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        host, port = listener.getsockname()
        accepted = []

        def _accept_once() -> None:
            conn, _addr = listener.accept()
            accepted.append(True)
            conn.close()
            listener.close()

        thread = threading.Thread(target=_accept_once, daemon=True)
        thread.start()

        service = CoreService(data_dir=self.data_dir)
        ok, message, entry = service.install_app(os.path.join(app_dir, "manifest.json"), app_dir)
        self.assertTrue(ok, message)
        self.assertIsNotNone(entry)
        assert entry is not None
        token = entry["token"]
        ok, message, _ = service.start_app("network-test")
        self.assertTrue(ok, message)
        http = HttpHarness(service)
        http.start()
        try:
            status, body = http.infer(
                "network-test",
                {"operation": "create_connection", "host": host, "port": port},
                token,
            )
            self.assertEqual(status, 200, body)
            self.assertTrue(body["result"]["network"]["allowed"], body)
        finally:
            http.stop()
            listener.close()
            thread.join(timeout=2)
        self.assertEqual(accepted, [True])


if __name__ == "__main__":
    unittest.main()
