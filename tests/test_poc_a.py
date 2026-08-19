"""PoC A tests — text-classifier (GeneralTask Day 2 list).

Cases: valid text; invalid text; missing field; oversized input; batch
request; model/hash mismatch (E5 at worker start -> not ready -> 503);
application restart (stop -> start -> infer OK again).

Each test works on a COPY of the bundle in a temp dir (isolated), with
real artifact hashes computed inside the test; nothing in examples/ is
modified.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import unittest

from sydeco_lightml_core.core import CoreService

from tests._http_harness import HttpHarness

BUNDLE = "/home/sydeco/Dev/SYDECO_LIGHTML_V2_DEV/examples/text-classifier"


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


class TestPocA(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="sydeco-poca-")
        self.bundle = os.path.join(self._tmp, "bundle")
        shutil.copytree(BUNDLE, self.bundle)
        self.data_dir = os.path.join(self._tmp, "data")
        self.service = CoreService(data_dir=self.data_dir)

        with open(os.path.join(self.bundle, "manifest.json"), "r", encoding="utf-8") as fh:
            manifest = json.load(fh)
        manifest["models"][0]["sha256"] = sha256_file(
            os.path.join(self.bundle, "models", "text_vectorizer.pkl")
        )
        manifest["models"][1]["sha256"] = sha256_file(
            os.path.join(self.bundle, "models", "text_model.pkl")
        )
        manifest["adapter"]["files"][0]["sha256"] = sha256_file(
            os.path.join(self.bundle, "adapter", "main.py")
        )
        with open(os.path.join(self.bundle, "manifest.json"), "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=2)

        ok, message, entry = self.service.install_app(
            os.path.join(self.bundle, "manifest.json"), app_root=self.bundle
        )
        self.assertTrue(ok, f"install failed: {message}")
        self.assertIsNotNone(entry)
        self.token = entry["token"]
        self.assertIsNotNone(self.token)

        ok, message, _ = self.service.start_app("text-classifier")
        self.assertTrue(ok, f"start failed: {message}")

        self.http = HttpHarness(self.service)
        self.http.start()

    def tearDown(self) -> None:
        self.http.stop()
        shutil.rmtree(self._tmp, ignore_errors=True)

    # 1. valid text
    def test_01_valid_text(self) -> None:
        status, body = self.http.infer(
            "text-classifier", {"text": "this product is great and excellent"}, self.token
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(body["app"], "text-classifier")
        self.assertIn("label", body["result"])
        self.assertIn("confidence", body["result"])
        conf = body["result"]["confidence"]
        self.assertGreaterEqual(conf, 0.0)
        self.assertLessEqual(conf, 1.0)
        self.assertIn(body["result"]["label"], ("positive", "negative"))

    # 2. invalid text (wrong type)
    def test_02_invalid_text(self) -> None:
        status, body = self.http.infer("text-classifier", {"text": 123}, self.token)
        self.assertEqual(status, 400, body)
        self.assertIn("error", body)
        self.assertNotIn("Traceback", str(body))

    # 3. missing field
    def test_03_missing_field(self) -> None:
        status, body = self.http.infer("text-classifier", {"foo": "bar"}, self.token)
        self.assertEqual(status, 400, body)
        self.assertIn("missing required field", body["error"]["message"])

    # 4. oversized input (maxLength 100000)
    def test_04_oversized_input(self) -> None:
        status, body = self.http.infer(
            "text-classifier", {"text": "a" * 100001}, self.token
        )
        self.assertEqual(status, 400, body)

    # 5. batch request (K5 / 5.2)
    def test_05_batch_request(self) -> None:
        status, body = self.http.infer(
            "text-classifier",
            {"inputs": [{"text": "this is great"}, {"text": "this is terrible"}]},
            self.token,
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(len(body["results"]), 2)
        self.assertIn("label", body["results"][0])
        self.assertIn("label", body["results"][1])
        self.assertEqual(body["app_version"], "1.0.0")

    # 6. model/hash mismatch (E5 at worker start)
    def test_06_model_hash_mismatch(self) -> None:
        # tamper with the model artifact AFTER install (on the temp copy)
        model_path = os.path.join(self.bundle, "models", "text_model.pkl")
        with open(model_path, "ab") as fh:
            fh.write(b"TAMPERED")
        ok, message, _ = self.service.start_app("text-classifier")
        self.assertFalse(ok, "worker must refuse to start on hash mismatch (E5)")
        self.assertIn("sha256 mismatch", message)
        # readiness must not be ready -> infer -> 503
        status, body = self.http.infer("text-classifier", {"text": "great"}, self.token)
        self.assertEqual(status, 503, body)
        self.assertEqual(body["error"]["code"], "503")

    # 7. application restart
    def test_07_app_restart(self) -> None:
        ok, _, _ = self.service.stop_app("text-classifier")
        self.assertTrue(ok)
        status, body = self.http.infer("text-classifier", {"text": "great"}, self.token)
        self.assertEqual(status, 503, body)  # stopped -> not ready
        ok, _, _ = self.service.start_app("text-classifier")
        self.assertTrue(ok)
        status, body = self.http.infer("text-classifier", {"text": "great"}, self.token)
        self.assertEqual(status, 200, body)
        self.assertIn("result", body)


if __name__ == "__main__":
    unittest.main()
