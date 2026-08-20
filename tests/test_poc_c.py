"""PoC C tests — document-triage pipeline (GeneralTask Day 3; proposal 7.3).

The workflow (preprocessing -> topic model -> rule -> sentiment model ->
aggregation) lives entirely in the bundle's Adapter; Core only loads the
models (E3 depends_on) and serves. Cases:
  valid full pipeline; low-confidence topic -> rule path "needs review";
  missing field; wrong type; oversized input; model/hash mismatch (E5 ->
  not ready -> 503); application restart.

Each test works on a COPY of the bundle in a temp dir (isolated), with
real artifact hashes computed inside the test and the bundle signed with
the TEST key (R6); nothing in examples/ is modified.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from sydeco_lightml_core.core import CoreService

from tests._http_harness import HttpHarness
from tests._signing import sign_manifest

REPO_ROOT = Path(__file__).resolve().parents[1]
BUNDLE = str(REPO_ROOT / "examples" / "document-triage")

TECHNICAL_TEXT = (
    "the server crashed during the deploy and the network failed, "
    "please fix this bug immediately"
)
GIBBERISH_TEXT = "asdf qwerty zxcv jklm"


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


class TestPocC(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="sydeco-pocc-")
        self.bundle = os.path.join(self._tmp, "bundle")
        shutil.copytree(BUNDLE, self.bundle)
        self.data_dir = os.path.join(self._tmp, "data")
        self.service = CoreService(data_dir=self.data_dir)

        with open(os.path.join(self.bundle, "manifest.json"), "r", encoding="utf-8") as fh:
            manifest = json.load(fh)
        for model in manifest["models"]:
            model["sha256"] = sha256_file(os.path.join(self.bundle, model["file"]))
        manifest["adapter"]["files"][0]["sha256"] = sha256_file(
            os.path.join(self.bundle, "adapter", "main.py")
        )
        with open(os.path.join(self.bundle, "manifest.json"), "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=2)
        sign_manifest(self.bundle)  # R6

        ok, message, entry = self.service.install_app(
            os.path.join(self.bundle, "manifest.json"), app_root=self.bundle
        )
        self.assertTrue(ok, f"install failed: {message}")
        self.assertIsNotNone(entry)
        self.token = entry["token"]
        self.assertIsNotNone(self.token)

        ok, message, _ = self.service.start_app("document-triage")
        self.assertTrue(ok, f"start failed: {message}")

        self.http = HttpHarness(self.service)
        self.http.start()

    def tearDown(self) -> None:
        self.http.stop()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _infer(self, payload: dict) -> tuple:
        return self.http.infer("document-triage", payload, self.token)

    # 1. valid text -> full pipeline -> structured result
    def test_01_valid_pipeline(self) -> None:
        status, body = self._infer({"text": TECHNICAL_TEXT})
        self.assertEqual(status, 200, body)
        result = body["result"]
        self.assertEqual(set(result), {"topic", "sentiment", "urgency", "confidence"})
        self.assertEqual(result["topic"], "technical")
        self.assertIn(result["sentiment"], ("positive", "negative"))
        self.assertIn(result["urgency"], ("low", "high", "review"))
        self.assertGreaterEqual(result["confidence"], 0.0)
        self.assertLessEqual(result["confidence"], 1.0)

    # 2. low-confidence topic -> rule path -> "needs review"
    def test_02_low_confidence_routes_needs_review(self) -> None:
        status, body = self._infer({"text": GIBBERISH_TEXT})
        self.assertEqual(status, 200, body)
        result = body["result"]
        self.assertEqual(result["topic"], "needs_review")
        self.assertEqual(result["urgency"], "review")

    # 3. missing field
    def test_03_missing_field(self) -> None:
        status, body = self._infer({"foo": "bar"})
        self.assertEqual(status, 400, body)
        self.assertIn("missing required field", body["error"]["message"])

    # 4. wrong type
    def test_04_wrong_type(self) -> None:
        status, body = self._infer({"text": 123})
        self.assertEqual(status, 400, body)

    # 5. oversized input (> maxLength 100000)
    def test_05_oversized_input(self) -> None:
        status, body = self._infer({"text": "a" * 100001})
        self.assertEqual(status, 400, body)

    # 6. model/hash mismatch (E5 at worker start)
    def test_06_model_hash_mismatch(self) -> None:
        model_path = os.path.join(self.bundle, "models", "triage_topic.pkl")
        with open(model_path, "ab") as fh:
            fh.write(b"TAMPERED")
        ok, message, _ = self.service.start_app("document-triage")
        self.assertFalse(ok, "worker must refuse to start on hash mismatch (E5)")
        self.assertIn("sha256 mismatch", message)
        status, body = self._infer({"text": TECHNICAL_TEXT})
        self.assertEqual(status, 503, body)

    # 7. application restart
    def test_07_app_restart(self) -> None:
        ok, _, _ = self.service.stop_app("document-triage")
        self.assertTrue(ok)
        status, _ = self._infer({"text": TECHNICAL_TEXT})
        self.assertEqual(status, 503)
        ok, _, _ = self.service.start_app("document-triage")
        self.assertTrue(ok)
        status, body = self._infer({"text": TECHNICAL_TEXT})
        self.assertEqual(status, 200, body)
        self.assertIn("result", body)


if __name__ == "__main__":
    unittest.main()
