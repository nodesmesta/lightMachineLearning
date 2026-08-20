"""PoC B tests — image-classifier (GeneralTask Day 2 list).

Cases: valid image (base64 PGM -> 200); malformed input (bad base64 ->
400 edge; non-PGM payload -> 500 sanitized); oversized payload (> 1 MiB
-> 400 at the edge); unavailable model (E5 at worker start -> 503);
application restart.

Each test works on a COPY of the bundle in a temp dir (isolated), with
real artifact hashes computed inside the test; nothing in examples/ is
modified.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest

from sydeco_lightml_core.core import CoreService

from tests._http_harness import HttpHarness, make_pgm

REPO_ROOT = Path(__file__).resolve().parents[1]
BUNDLE = str(REPO_ROOT / "examples" / "image-classifier")


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def b64_image(pixels: list) -> str:
    return base64.b64encode(make_pgm(4, 4, pixels)).decode("ascii")


class TestPocB(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="sydeco-pocb-")
        self.bundle = os.path.join(self._tmp, "bundle")
        shutil.copytree(BUNDLE, self.bundle)
        self.data_dir = os.path.join(self._tmp, "data")
        self.service = CoreService(data_dir=self.data_dir)

        with open(os.path.join(self.bundle, "manifest.json"), "r", encoding="utf-8") as fh:
            manifest = json.load(fh)
        manifest["models"][0]["sha256"] = sha256_file(
            os.path.join(self.bundle, "models", "image_model.joblib")
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

        ok, message, _ = self.service.start_app("image-classifier")
        self.assertTrue(ok, f"start failed: {message}")

        self.http = HttpHarness(self.service)
        self.http.start()

    def tearDown(self) -> None:
        self.http.stop()
        shutil.rmtree(self._tmp, ignore_errors=True)

    # 1. valid image (dark -> "dark")
    def test_01_valid_image_dark(self) -> None:
        status, body = self.http.infer(
            "image-classifier", {"image": b64_image([0] * 16)}, self.token
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(body["result"]["label"], "dark")
        self.assertGreaterEqual(body["result"]["confidence"], 0.5)

    # 1b. valid image (bright -> "bright")
    def test_01b_valid_image_bright(self) -> None:
        status, body = self.http.infer(
            "image-classifier", {"image": b64_image([255] * 16)}, self.token
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(body["result"]["label"], "bright")
        self.assertGreaterEqual(body["result"]["confidence"], 0.5)

    # 2. malformed input: invalid base64 -> 400 at the edge (H3)
    def test_02_malformed_base64(self) -> None:
        status, body = self.http.infer(
            "image-classifier", {"image": "!!!not-base64!!!"}, self.token
        )
        self.assertEqual(status, 400, body)
        self.assertIn("base64", body["error"]["message"])

    # 2b. malformed input: valid base64 but NOT a PGM -> 500 sanitized
    def test_02b_non_pgm_payload(self) -> None:
        bad = base64.b64encode(b"this is not a pgm image at all").decode("ascii")
        status, body = self.http.infer("image-classifier", {"image": bad}, self.token)
        self.assertEqual(status, 500, body)
        # sanitized: generic message, no exception detail
        self.assertEqual(body["error"]["message"], "internal error")
        self.assertNotIn("ValueError", str(body))
        self.assertNotIn("PGM", str(body))

    # 3. oversized payload (> 1 MiB body limit, K5/H3) -> 400 at the edge
    def test_03_oversized_payload(self) -> None:
        # Deterministic on 3.10 AND 3.13 (reviewer directive 20-08-2026):
        # declare Content-Length > 1 MiB but transmit NO oversized body;
        # the server rejects on the DECLARED length before reading the
        # body (edge, H3) -> 400, so the client never hits a broken pipe.
        status, body = self.http.request_declared(
            "POST",
            "/api/v1/apps/image-classifier/infer",
            declared_content_length=1_500_000,
            token=self.token,
        )
        self.assertEqual(status, 400, body)
        self.assertIn("size limit", body["error"]["message"])

    # 4. unavailable model (artifact removed after install -> E5 -> 503)
    def test_04_unavailable_model(self) -> None:
        os.remove(os.path.join(self.bundle, "models", "image_model.joblib"))
        ok, message, _ = self.service.start_app("image-classifier")
        self.assertFalse(ok, "worker must refuse to start when artifact missing (E5)")
        self.assertIn("missing", message)
        status, body = self.http.infer(
            "image-classifier", {"image": b64_image([0] * 16)}, self.token
        )
        self.assertEqual(status, 503, body)

    # 5. application restart
    def test_05_app_restart(self) -> None:
        ok, _, _ = self.service.stop_app("image-classifier")
        self.assertTrue(ok)
        status, _ = self.http.infer(
            "image-classifier", {"image": b64_image([0] * 16)}, self.token
        )
        self.assertEqual(status, 503)
        ok, _, _ = self.service.start_app("image-classifier")
        self.assertTrue(ok)
        status, body = self.http.infer(
            "image-classifier", {"image": b64_image([0] * 16)}, self.token
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(body["result"]["label"], "dark")


if __name__ == "__main__":
    unittest.main()
