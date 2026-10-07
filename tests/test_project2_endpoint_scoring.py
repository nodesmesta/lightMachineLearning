"""Unit and integration test for project2-endpoint-scoring bundle (P1).

Verifies authentic RFC 8032 Ed25519 signature verification, fail-closed
tamper detection, artifact hash integrity, and inference execution
via DefaultModelAdapter on the 12-feature endpoint telemetry model.
"""
from __future__ import annotations

import copy
import json
import os
import sys
import time
import unittest

BUNDLE_DIR = "/home/sydeco/Dev/SYDECO_LIGHTML_V2_DEV/lib/project2-endpoint-scoring"
LIGHTML_ROOT = "/home/sydeco/Dev/SYDECO_LIGHTML_V2_DEV"

if BUNDLE_DIR not in sys.path:
    sys.path.insert(0, BUNDLE_DIR)
if LIGHTML_ROOT not in sys.path:
    sys.path.insert(0, LIGHTML_ROOT)

from sydeco_lightml_core.adapter import DefaultModelAdapter
from sydeco_lightml_core.keys import (
    DEFAULT_DEV_KEY_ID,
    DEFAULT_DEV_PUBKEY_PEM,
    verify_bundle_signature,
)
from sydeco_lightml_core.loader import load_artifacts, verify_artifact_hashes
from sydeco_lightml_core.validation import validate_manifest


class TestProject2EndpointScoringBundle(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest_path = os.path.join(BUNDLE_DIR, "manifest.json")
        cls.sig_path = os.path.join(BUNDLE_DIR, "manifest.sig")
        cls.model_path = os.path.join(BUNDLE_DIR, "model.pkl")

        with open(cls.manifest_path, "r", encoding="utf-8") as f:
            cls.manifest = json.load(f)

        # Ensure trusted key store exists for test
        cls.trust_dir = "/tmp/sydeco_test_trust"
        os.makedirs(cls.trust_dir, exist_ok=True)
        with open(os.path.join(cls.trust_dir, "trusted_keys.json"), "w", encoding="utf-8") as f:
            json.dump({"keys": {DEFAULT_DEV_KEY_ID: DEFAULT_DEV_PUBKEY_PEM}}, f)

    def test_01_manifest_schema_validation(self) -> None:
        ok, errors = validate_manifest(self.manifest)
        self.assertTrue(ok, f"Manifest schema validation failed: {errors}")
        self.assertEqual(self.manifest["app_id"], "project2-endpoint-scoring")
        self.assertEqual(self.manifest["version"], "1.0.0")
        self.assertEqual(self.manifest["permissions"]["network"], "none")

    def test_02_authentic_ed25519_signature(self) -> None:
        ok, reason = verify_bundle_signature(
            self.manifest, self.sig_path, data_dir=self.trust_dir
        )
        self.assertTrue(ok, f"Ed25519 signature verification failed: {reason}")

    def test_03_tampered_manifest_rejected_fail_closed(self) -> None:
        tampered = copy.deepcopy(self.manifest)
        tampered["name"] = "Tampered Model Name"
        ok, reason = verify_bundle_signature(
            tampered, self.sig_path, data_dir=self.trust_dir
        )
        self.assertFalse(ok, "Fail-closed check failed: tampered manifest was accepted!")
        self.assertIn("signature verification failed", reason)

    def test_04_artifact_hashes_integrity(self) -> None:
        hashes = verify_artifact_hashes(self.manifest, BUNDLE_DIR)
        self.assertIn("model.pkl", hashes)
        self.assertEqual(hashes["model.pkl"], self.manifest["models"][0]["sha256"])

    def test_05_tampered_model_hash_rejected(self) -> None:
        tampered = copy.deepcopy(self.manifest)
        tampered["models"][0]["sha256"] = "f" * 64
        with self.assertRaises(Exception):
            verify_artifact_hashes(tampered, BUNDLE_DIR)

    def test_06_default_adapter_inference_normal_and_anomalous(self) -> None:
        artifacts = load_artifacts(self.manifest, BUNDLE_DIR)
        self.assertIn("primary", artifacts)

        adapter = DefaultModelAdapter()
        adapter.initialize({"models": artifacts, "config": self.manifest})

        # Normal sample: standard bash session by regular user
        normal_vec = [0.05, 0.01, 0.15, 0.04, 0.30, 1.0, 0.05, 0.02, 0.01, 0.02, 0.01, 0.0]
        res_norm = adapter.infer({"features": normal_vec}, {"request_id": "test-norm"})
        self.assertEqual(res_norm["status"], "ok")
        normal_score = res_norm["result"][0][1]
        self.assertLess(normal_score, 0.30, f"Normal sample scored too high: {normal_score}")

        # Anomalous sample: privilege escalation, high entropy cmdline, abnormal open FDs & threads
        anom_vec = [0.95, 0.02, 0.90, 0.85, 0.95, 0.0, 0.001, 0.80, 0.75, 0.60, 0.50, 1.0]
        res_anom = adapter.infer({"features": anom_vec}, {"request_id": "test-anom"})
        self.assertEqual(res_anom["status"], "ok")
        anom_score = res_anom["result"][0][1]
        self.assertGreater(anom_score, 0.75, f"Anomalous sample scored too low: {anom_score}")

        # Benchmark latency SLA < 2.5 ms
        t0 = time.perf_counter()
        for _ in range(100):
            adapter.infer({"features": normal_vec}, {"request_id": "bench"})
        elapsed_ms_per_call = (time.perf_counter() - t0) * 10
        self.assertLess(
            elapsed_ms_per_call, 2.5, f"Inference latency exceeded 2.5 ms SLA: {elapsed_ms_per_call} ms"
        )

        adapter.shutdown()


if __name__ == "__main__":
    unittest.main()
