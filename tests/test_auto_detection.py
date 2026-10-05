"""Unit tests for Drop-In Bundle Auto-Detection and safe extraction (P2)."""
from __future__ import annotations

import json
import os
import pickle
import shutil
import tempfile
import unittest
import zipfile

import sydeco_lightml_core.core
from sydeco_lightml_core.core import (
    CoreService,
    _safe_extract_zip,
    _sha256_file,
)
from sydeco_lightml_core.keys import sign_manifest_canonical

DEV_KEY_PATH = os.path.join(
    os.path.dirname(__file__), "..", "devkeys", "DEVELOPMENT_TEST_KEY_DO_NOT_USE_IN_PRODUCTION.pem"
)
DEV_PUB_KEY_PATH = os.path.join(
    os.path.dirname(__file__), "..", "devkeys", "DEVELOPMENT_TEST_KEY_DO_NOT_USE_IN_PRODUCTION.pub.pem"
)


class MockScikitModel:
    def predict(self, X):
        return [1 if x > 0.5 else 0 for x in X]


class TestAutoDetection(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp(prefix="test_autodetect_")
        self.data_dir = os.path.join(self.temp_dir, "data")
        os.makedirs(self.data_dir, exist_ok=True)
        with open(DEV_PUB_KEY_PATH, "r", encoding="ascii") as fh:
            pub_pem = fh.read()
        with open(os.path.join(self.data_dir, "trusted_keys.json"), "w", encoding="utf-8") as fh:
            json.dump({"sydeco-test-key-v1": pub_pem}, fh)
        self.service = CoreService(data_dir=self.data_dir, worker_mode="inprocess")

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _create_sample_bundle_dir(self, app_id: str = "auto-test-app") -> str:
        """Create a valid zero-code bundle directory with a valid signature placeholder."""
        bundle_dir = os.path.join(self.temp_dir, f"bundle_{app_id}")
        os.makedirs(bundle_dir, exist_ok=True)
        models_dir = os.path.join(bundle_dir, "models")
        os.makedirs(models_dir, exist_ok=True)

        # 1. Model artifact
        model_path = os.path.join(models_dir, "model.pkl")
        with open(model_path, "wb") as fh:
            pickle.dump(MockScikitModel(), fh)
        model_sha = _sha256_file(model_path)

        # 2. Manifest
        manifest = {
            "manifest_version": 1,
            "app_id": app_id,
            "name": f"Auto Test {app_id}",
            "version": "1.0.0",
            "capabilities": ["inference"],
            "models": [
                {
                    "role": "primary",
                    "file": "models/model.pkl",
                    "format": "pickle",
                    "sha256": model_sha,
                }
            ],
            "release": {
                "key_id": "sydeco-test-key-v1",
                "signature": "manifest.sig",
            },
            "input_schema": {"type": "object"},
            "output_schema": {"type": "object"},
            "permissions": {"network": "none"},
            "api": {"authentication": "token"},
            "resource_limits": {
                "max_memory": 268435456,
                "max_cpu": 50,
                "inference_timeout": 30,
                "concurrency": 1,
            },
        }
        manifest_path = os.path.join(bundle_dir, "manifest.json")
        with open(manifest_path, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=2)

        # 3. Signature file (real Ed25519 signature)
        with open(DEV_KEY_PATH, "r", encoding="ascii") as fh:
            priv_pem = fh.read()
        sig_b64 = sign_manifest_canonical(manifest, priv_pem)
        sig_path = os.path.join(bundle_dir, "manifest.sig")
        with open(sig_path, "w", encoding="ascii") as fh:
            fh.write(sig_b64 + "\n")

        return bundle_dir

    def _create_zip_from_dir(self, source_dir: str, zip_path: str) -> str:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for root, _, files in os.walk(source_dir):
                for f in files:
                    full = os.path.join(root, f)
                    rel = os.path.relpath(full, source_dir)
                    zf.write(full, rel)
        return zip_path

    def test_safe_extract_zip_prevents_traversal(self) -> None:
        """Verify Zip Slip path traversal attempt is detected and rejected."""
        malicious_zip = os.path.join(self.temp_dir, "malicious.zip")
        with zipfile.ZipFile(malicious_zip, "w") as zf:
            zf.writestr("../../evil.txt", "malicious payload")

        dest = os.path.join(self.temp_dir, "extract_dest")
        with self.assertRaises(RuntimeError) as cm:
            _safe_extract_zip(malicious_zip, dest)
        self.assertIn("Zip Slip path traversal attempt detected", str(cm.exception))

    def test_install_bundle_zip(self) -> None:
        """Verify install_bundle successfully unzips, registers, and starts worker."""
        bundle_dir = self._create_sample_bundle_dir("test-zip-install")
        zip_path = os.path.join(self.temp_dir, "test-zip-install.zip")
        self._create_zip_from_dir(bundle_dir, zip_path)

        ok, msg, entry = self.service.install_bundle(zip_path)
        self.assertTrue(ok, f"install_bundle failed: {msg}")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["app_id"], "test-zip-install")

        # Verify app is registered and ready in registry
        app_info = self.service.registry.get("test-zip-install")
        self.assertIsNotNone(app_info)
        self.assertEqual(app_info["status"], "active")
        self.assertEqual(self.service.readiness.status("test-zip-install"), "ready")

    def test_scan_incoming_dropin(self) -> None:
        """Verify dropping a .zip into incoming/ triggers automatic discovery and deployment."""
        incoming_dir = os.path.join(self.data_dir, "incoming")
        os.makedirs(incoming_dir, exist_ok=True)

        bundle_dir = self._create_sample_bundle_dir("dropin-app")
        dropin_zip = os.path.join(incoming_dir, "dropin-app-1.0.0.zip")
        self._create_zip_from_dir(bundle_dir, dropin_zip)

        # Ensure file exists in incoming/
        self.assertTrue(os.path.isfile(dropin_zip))

        # Run scan_incoming
        results = self.service.scan_incoming()
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["ok"], f"scan_incoming failed: {results[0].get('message')}")
        self.assertEqual(results[0]["file"], "dropin-app-1.0.0.zip")

        # Verify archive moved to incoming/processed/
        processed_path = os.path.join(incoming_dir, "processed", "dropin-app-1.0.0.zip")
        self.assertTrue(os.path.isfile(processed_path))
        self.assertFalse(os.path.isfile(dropin_zip))

        # Verify app is running in registry
        app_info = self.service.registry.get("dropin-app")
        self.assertIsNotNone(app_info)
        self.assertEqual(app_info["status"], "active")
        self.assertEqual(self.service.readiness.status("dropin-app"), "ready")


if __name__ == "__main__":
    unittest.main()
