"""Isolation test (GeneralTask Day 2; proposal 2.2/G2 + 2.3/A3).

Deliberately break one PoC worker (simulated crash, dev test hook); the
other PoC must remain operational. Both PoCs run under the SAME Core
service (two independent applications, one Core).

Flow:
  1. install + start BOTH PoCs (on copies of the bundles, isolated)
  2. both ready; infer on both -> 200
  3. crash text-classifier -> its readiness becomes backoff
  4. infer text-classifier -> 503 (broken app never takes healthy apps
     down with it, G2/A3)
  5. infer image-classifier -> 200 (still operational)
  6. restart text-classifier -> ready again -> both serve
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

BASE_EXAMPLES = "/home/sydeco/Dev/SYDECO_LIGHTML_V2_DEV/examples"
BUNDLES = {
    "text-classifier": os.path.join(BASE_EXAMPLES, "text-classifier"),
    "image-classifier": os.path.join(BASE_EXAMPLES, "image-classifier"),
}


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _patch_manifest_hashes(bundle: str) -> None:
    with open(os.path.join(bundle, "manifest.json"), "r", encoding="utf-8") as fh:
        manifest = json.load(fh)
    for model in manifest["models"]:
        model["sha256"] = sha256_file(os.path.join(bundle, model["file"]))
    for f in manifest["adapter"]["files"]:
        f["sha256"] = sha256_file(os.path.join(bundle, f["file"]))
    with open(os.path.join(bundle, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)


class TestIsolation(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="sydeco-iso-")
        self.data_dir = os.path.join(self._tmp, "data")
        self.service = CoreService(data_dir=self.data_dir)
        self.tokens: dict = {}
        for app_id, src in BUNDLES.items():
            bundle = os.path.join(self._tmp, app_id)
            shutil.copytree(src, bundle)
            _patch_manifest_hashes(bundle)
            ok, message, entry = self.service.install_app(
                os.path.join(bundle, "manifest.json"), app_root=bundle
            )
            self.assertTrue(ok, f"install {app_id} failed: {message}")
            self.assertIsNotNone(entry)
            self.tokens[app_id] = entry["token"]
            ok, message, _ = self.service.start_app(app_id)
            self.assertTrue(ok, f"start {app_id} failed: {message}")
        self.http = HttpHarness(self.service)
        self.http.start()

    def tearDown(self) -> None:
        self.http.stop()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _health(self) -> dict:
        status, body = self.http.get("/health/apps")
        self.assertEqual(status, 200, body)
        return body

    def test_01_both_ready(self) -> None:
        health = self._health()
        self.assertEqual(health["text-classifier"]["status"], "ready")
        self.assertEqual(health["image-classifier"]["status"], "ready")

    def test_02_break_a_b_stays_up(self) -> None:
        # both serve before the crash
        status, body = self.http.infer(
            "text-classifier", {"text": "great"}, self.tokens["text-classifier"]
        )
        self.assertEqual(status, 200, body)
        status, _ = self.http.infer(
            "image-classifier",
            {"image": __import__("base64").b64encode(
                b"P5\n4 4\n255\n" + bytes([0] * 16)).decode()},
            self.tokens["image-classifier"],
        )
        self.assertEqual(status, 200)

        # deliberately break worker A (dev test hook)
        ok, message, _ = self.service.crash_app("text-classifier")
        self.assertTrue(ok, message)

        health = self._health()
        self.assertEqual(health["text-classifier"]["status"], "backoff")
        self.assertEqual(health["image-classifier"]["status"], "ready")

        # broken app -> 503, does NOT take the healthy app down (A3/G2)
        status, body = self.http.infer(
            "text-classifier", {"text": "great"}, self.tokens["text-classifier"]
        )
        self.assertEqual(status, 503, body)
        self.assertEqual(body["error"]["code"], "503")

        status, body = self.http.infer(
            "image-classifier",
            {"image": __import__("base64").b64encode(
                b"P5\n4 4\n255\n" + bytes([0] * 16)).decode()},
            self.tokens["image-classifier"],
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(body["result"]["label"], "dark")

        # recover A -> both serve again
        ok, message, _ = self.service.start_app("text-classifier")
        self.assertTrue(ok, message)
        status, body = self.http.infer(
            "text-classifier", {"text": "great"}, self.tokens["text-classifier"]
        )
        self.assertEqual(status, 200, body)
        status, _ = self.http.infer(
            "image-classifier",
            {"image": __import__("base64").b64encode(
                b"P5\n4 4\n255\n" + bytes([255] * 16)).decode()},
            self.tokens["image-classifier"],
        )
        self.assertEqual(status, 200)


if __name__ == "__main__":
    unittest.main()
