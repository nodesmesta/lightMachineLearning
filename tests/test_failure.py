"""Failure-test suite (GeneralTask Day 3; errors to clients SANITIZED).

10 cases:
  1. adapter initialization failure   -> start refused -> 503
  2. adapter inference exception      -> 500 sanitized ("internal error")
  3. inference timeout (M4)           -> 504
  4. worker crash (dev simulate_crash)-> 503
  5. malformed input                  -> 400
  6. output not matching output_schema (H2) -> 500 sanitized
  7. model hash mismatch (E5 at start)-> not ready -> 503
  8. adapter hash mismatch (R5/E5 at start) -> not ready -> 503
  9. application unavailable          -> 404
 10. one app fails while another stays healthy -> 503 vs 200

Every client-visible error is the sanitized P4 envelope; full detail
stays server-side (tracebacks in the server log are expected behaviour).
"""
from __future__ import annotations

import hashlib
import json
import os
import pickle
import shutil
import tempfile
import unittest

from sydeco_lightml_core.core import CoreService

from tests._http_harness import HttpHarness
from tests._signing import sign_manifest

ADAPTERS = {
    "normal": (
        "class Adapter:\n"
        "    def initialize(self, context):\n"
        "        pass\n"
        "    def infer(self, request, context):\n"
        "        return {'ok': True}\n"
        "    def shutdown(self):\n"
        "        pass\n"
    ),
    "initfail": (
        "class Adapter:\n"
        "    def initialize(self, context):\n"
        "        raise RuntimeError('adapter init failure')\n"
        "    def infer(self, request, context):\n"
        "        return {'ok': True}\n"
        "    def shutdown(self):\n"
        "        pass\n"
    ),
    "inferraise": (
        "class Adapter:\n"
        "    def initialize(self, context):\n"
        "        pass\n"
        "    def infer(self, request, context):\n"
        "        raise ValueError('boom-infer')\n"
        "    def shutdown(self):\n"
        "        pass\n"
    ),
    "inferslow": (
        "import time\n"
        "class Adapter:\n"
        "    def initialize(self, context):\n"
        "        pass\n"
        "    def infer(self, request, context):\n"
        "        time.sleep(5)\n"
        "        return {'ok': True}\n"
        "    def shutdown(self):\n"
        "        pass\n"
    ),
    "wrongout": (
        "class Adapter:\n"
        "    def initialize(self, context):\n"
        "        pass\n"
        "    def infer(self, request, context):\n"
        "        return {'bad': 1}\n"
        "    def shutdown(self):\n"
        "        pass\n"
    ),
}

MODEL_BYTES = pickle.dumps({"dummy": True})


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _make_bundle(tmp: str, kind: str, timeout: float = 120.0) -> str:
    code = ADAPTERS[kind]
    d = os.path.join(tmp, f"fail-{kind}")
    os.makedirs(os.path.join(d, "models"), exist_ok=True)
    os.makedirs(os.path.join(d, "adapter"), exist_ok=True)
    with open(os.path.join(d, "models", "model.pkl"), "wb") as fh:
        fh.write(MODEL_BYTES)
    with open(os.path.join(d, "adapter", "main.py"), "w", encoding="utf-8") as fh:
        fh.write(code)
    manifest = {
        "manifest_version": 1,
        "app_id": f"fail-{kind}",
        "name": f"Failure {kind}",
        "version": "1.0.0",
        "capabilities": ["inference"],
        "models": [
            {
                "role": "model",
                "file": "models/model.pkl",
                "format": "pickle",
                "sha256": _sha256(MODEL_BYTES),
            }
        ],
        "adapter": {
            "entry": "adapter/main.py",
            "files": [{"file": "adapter/main.py", "sha256": _sha256(code.encode())}],
        },
        "release": {"key_id": "sydeco-test-key-v1", "signature": "manifest.sig"},
        "input_schema": {
            "type": "object",
            "required": ["text"],
            "properties": {"text": {"type": "string"}},
        },
        "output_schema": {
            "type": "object",
            "required": ["ok"],
            "properties": {"ok": {"type": "boolean"}},
        },
        "permissions": {"network": "none"},
        "api": {"authentication": "token"},
        "resource_limits": {
            "max_memory": 1073741824,
            "max_cpu": 100,
            "inference_timeout": timeout,
            "concurrency": 1,
        },
        "dependencies": [],
    }
    with open(os.path.join(d, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)
    sign_manifest(d)
    return d


class TestFailure(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="sydeco-fail-")
        self.data_dir = os.path.join(self._tmp, "data")
        self.service = CoreService(data_dir=self.data_dir)
        self.http = HttpHarness(self.service)
        self.http.start()

    def tearDown(self) -> None:
        self.http.stop()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _install_start(self, kind: str, timeout: float = 120.0) -> tuple:
        bundle = _make_bundle(self._tmp, kind, timeout)
        ok, message, entry = self.service.install_app(
            os.path.join(bundle, "manifest.json"), app_root=bundle
        )
        self.assertTrue(ok, f"install {kind} failed: {message}")
        app_id = f"fail-{kind}"
        ok, message, _ = self.service.start_app(app_id)
        self.assertTrue(ok, f"start {kind} failed: {message}")
        return app_id, entry["token"]  # token returned once at install (K2/R8)

    def _infer(self, app_id: str, payload: dict, token: str) -> tuple:
        return self.http.infer(app_id, payload, token)

    # 1. adapter initialization failure -> start refused -> 503 (sanitized)
    def test_01_adapter_initialization_failure(self) -> None:
        bundle = _make_bundle(self._tmp, "initfail")
        ok, message, entry = self.service.install_app(
            os.path.join(bundle, "manifest.json"), app_root=bundle
        )
        self.assertTrue(ok, message)
        ok, message, _ = self.service.start_app("fail-initfail")
        self.assertFalse(ok, "start must fail when adapter.initialize raises")
        self.assertIn("adapter init failure", message)  # server-side detail only
        status, body = self._infer(
            "fail-initfail", {"text": "x"}, entry["token"]
        )
        self.assertEqual(status, 503, body)
        self.assertNotIn("adapter init failure", str(body))  # sanitized
        self.assertNotIn("Traceback", str(body))

    # 2. adapter inference exception -> 500 sanitized
    def test_02_adapter_inference_exception(self) -> None:
        app_id, token = self._install_start("inferraise")
        status, body = self._infer(app_id, {"text": "x"}, token)
        self.assertEqual(status, 500, body)
        self.assertEqual(body["error"]["message"], "internal error")
        self.assertNotIn("boom-infer", str(body))  # sanitized
        self.assertNotIn("ValueError", str(body))
        self.assertNotIn("Traceback", str(body))

    # 3. inference timeout (M4) -> 504
    def test_03_inference_timeout(self) -> None:
        app_id, token = self._install_start("inferslow", timeout=1.0)
        status, body = self._infer(app_id, {"text": "x"}, token)
        self.assertEqual(status, 504, body)
        self.assertEqual(body["error"]["code"], "504")

    # 4. worker crash (dev simulate_crash hook) -> 503
    def test_04_worker_crash(self) -> None:
        app_id, token = self._install_start("normal")
        ok, _, _ = self.service.crash_app(app_id)
        self.assertTrue(ok)
        status, body = self._infer(app_id, {"text": "x"}, token)
        self.assertEqual(status, 503, body)
        self.assertEqual(body["error"]["code"], "503")

    # 5. malformed input -> 400 at the edge (H3)
    def test_05_malformed_input(self) -> None:
        app_id, token = self._install_start("normal")
        status, body = self._infer(app_id, {"text": 123}, token)
        self.assertEqual(status, 400, body)

    # 6. output not matching output_schema (H2) -> 500 sanitized
    def test_06_output_not_matching_schema(self) -> None:
        app_id, token = self._install_start("wrongout")
        status, body = self._infer(app_id, {"text": "x"}, token)
        self.assertEqual(status, 500, body)
        self.assertEqual(body["error"]["message"], "internal error")
        self.assertNotIn("Traceback", str(body))

    # 7. model hash mismatch (E5 at worker start) -> not ready -> 503
    def test_07_model_hash_mismatch(self) -> None:
        bundle = _make_bundle(self._tmp, "normal")
        ok, message, entry = self.service.install_app(
            os.path.join(bundle, "manifest.json"), app_root=bundle
        )
        self.assertTrue(ok, message)
        with open(os.path.join(bundle, "models", "model.pkl"), "ab") as fh:
            fh.write(b"TAMPERED")
        ok, message, _ = self.service.start_app("fail-normal")
        self.assertFalse(ok, "start must refuse on model hash mismatch (E5)")
        self.assertIn("sha256 mismatch", message)
        status, body = self._infer("fail-normal", {"text": "x"}, entry["token"])
        self.assertEqual(status, 503, body)

    # 8. adapter hash mismatch (R5/E5 at worker start) -> not ready -> 503
    def test_08_adapter_hash_mismatch(self) -> None:
        bundle = _make_bundle(self._tmp, "normal")
        ok, message, entry = self.service.install_app(
            os.path.join(bundle, "manifest.json"), app_root=bundle
        )
        self.assertTrue(ok, message)
        with open(os.path.join(bundle, "adapter", "main.py"), "a", encoding="utf-8") as fh:
            fh.write("# tampered\n")
        ok, message, _ = self.service.start_app("fail-normal")
        self.assertFalse(ok, "start must refuse on adapter hash mismatch (R5)")
        self.assertIn("adapter sha256 mismatch", message)
        status, body = self._infer("fail-normal", {"text": "x"}, entry["token"])
        self.assertEqual(status, 503, body)

    # 9. application unavailable -> 404
    def test_09_application_unavailable(self) -> None:
        status, body = self._infer("no-such-app", {"text": "x"}, "whatever")
        self.assertEqual(status, 404, body)
        self.assertEqual(body["error"]["code"], "404")

    # 10. one app fails while another remains healthy (A3/G2)
    def test_10_one_app_fails_another_healthy(self) -> None:
        app_a, token_a = self._install_start("normal")
        # second app with a distinct id (fresh bundle)
        bundle_b = _make_bundle(self._tmp, "normal")
        # give app B a distinct app_id by patching the copy
        b_dir = os.path.join(self._tmp, "fail-normal-b")
        os.makedirs(os.path.join(b_dir, "models"), exist_ok=True)
        os.makedirs(os.path.join(b_dir, "adapter"), exist_ok=True)
        shutil.copy(
            os.path.join(bundle_b, "models", "model.pkl"),
            os.path.join(b_dir, "models", "model.pkl"),
        )
        with open(os.path.join(bundle_b, "adapter", "main.py"), "r", encoding="utf-8") as fh:
            code = fh.read()
        with open(os.path.join(b_dir, "adapter", "main.py"), "w", encoding="utf-8") as fh:
            fh.write(code)
        with open(os.path.join(bundle_b, "manifest.json"), "r", encoding="utf-8") as fh:
            manifest = json.load(fh)
        manifest["app_id"] = "fail-normal-b"
        manifest["models"][0]["sha256"] = _sha256(MODEL_BYTES)
        manifest["adapter"]["files"][0]["sha256"] = _sha256(code.encode())
        with open(os.path.join(b_dir, "manifest.json"), "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=2)
        sign_manifest(b_dir)
        ok, message, entry_b = self.service.install_app(
            os.path.join(b_dir, "manifest.json"), app_root=b_dir
        )
        self.assertTrue(ok, message)
        ok, message, _ = self.service.start_app("fail-normal-b")
        self.assertTrue(ok, message)

        # both healthy
        status, _ = self._infer(app_a, {"text": "x"}, token_a)
        self.assertEqual(status, 200)
        status, _ = self._infer("fail-normal-b", {"text": "x"}, entry_b["token"])
        self.assertEqual(status, 200)

        # break app A
        self.service.crash_app(app_a)
        status, body = self._infer(app_a, {"text": "x"}, token_a)
        self.assertEqual(status, 503, body)
        status, body = self._infer(
            "fail-normal-b", {"text": "x"}, entry_b["token"]
        )
        self.assertEqual(status, 200, body)  # other app stays healthy


if __name__ == "__main__":
    unittest.main()
