"""Comprehensive Security & Trust Verification Suite (P1).

Validates Mandatory Trust (Ed25519 signatures, fail-closed verification)
and Edge Caller Authentication (salted SHA-256 tokens, isolation, rotation)
without ANY test mocks or stubs.
"""
from __future__ import annotations

import base64
import json
import os
import pickle
import shutil
import tempfile
import unittest

from sydeco_lightml_core.core import CoreService, _sha256_file
from sydeco_lightml_core.keys import sign_manifest_canonical
from sydeco_lightml_core.secrets import read_token, rotate_token, verify_token
from sydeco_lightml_core.server import CoreHTTPServer

DEV_KEY_PATH = os.path.join(
    os.path.dirname(__file__), "..", "devkeys", "DEVELOPMENT_TEST_KEY_DO_NOT_USE_IN_PRODUCTION.pem"
)


class MockModel:
    def predict(self, X):
        return [1 for _ in X]


class TestSecurityTrust(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp(prefix="test_sec_trust_")
        self.data_dir = os.path.join(self.temp_dir, "data")
        os.makedirs(self.data_dir, exist_ok=True)
        dev_pub_path = os.path.join(
            os.path.dirname(__file__), "..", "devkeys", "DEVELOPMENT_TEST_KEY_DO_NOT_USE_IN_PRODUCTION.pub.pem"
        )
        with open(dev_pub_path, "r", encoding="ascii") as fh:
            pub_pem = fh.read()
        with open(os.path.join(self.data_dir, "trusted_keys.json"), "w", encoding="utf-8") as fh:
            json.dump({"sydeco-test-key-v1": pub_pem}, fh)
        self.service = CoreService(data_dir=self.data_dir, worker_mode="inprocess")

        with open(DEV_KEY_PATH, "r", encoding="ascii") as fh:
            self.priv_pem = fh.read()

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _create_bundle(self, app_id: str, sign: bool = True, tamper: bool = False, key_id: str = "sydeco-test-key-v1") -> str:
        app_dir = os.path.join(self.temp_dir, f"bundle_{app_id}")
        os.makedirs(os.path.join(app_dir, "models"), exist_ok=True)

        model_path = os.path.join(app_dir, "models", "model.pkl")
        with open(model_path, "wb") as fh:
            pickle.dump(MockModel(), fh)
        model_sha = _sha256_file(model_path)

        manifest = {
            "manifest_version": 1,
            "app_id": app_id,
            "name": f"Security Test {app_id}",
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
                "key_id": key_id,
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

        manifest_path = os.path.join(app_dir, "manifest.json")
        with open(manifest_path, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=2)

        if sign:
            sig_b64 = sign_manifest_canonical(manifest, self.priv_pem)
            if tamper:
                # Alter first byte of base64 signature
                sig_b64 = ("A" if sig_b64[0] != "A" else "B") + sig_b64[1:]
            with open(os.path.join(app_dir, "manifest.sig"), "w", encoding="ascii") as fh:
                fh.write(sig_b64 + "\n")

        return app_dir

    def test_sec_01_valid_signature_installs_successfully(self) -> None:
        """TC-SEC-01: Valid signed bundle with trusted key installs and starts successfully."""
        app_dir = self._create_bundle("valid-signed-app", sign=True)
        manifest_path = os.path.join(app_dir, "manifest.json")
        ok, msg, entry = self.service.install_app(manifest_path, app_root=app_dir)
        self.assertTrue(ok, f"Expected successful install, got: {msg}")
        self.assertIsNotNone(entry)
        self.assertIn("token", entry)

    def test_sec_02_unsigned_bundle_rejected_fail_closed(self) -> None:
        """TC-SEC-02: Bundle without manifest.sig is rejected fail-closed."""
        app_dir = self._create_bundle("unsigned-app", sign=False)
        manifest_path = os.path.join(app_dir, "manifest.json")
        ok, msg, entry = self.service.install_app(manifest_path, app_root=app_dir)
        self.assertFalse(ok)
        self.assertIn("cannot read signature file", msg)

    def test_sec_03_tampered_manifest_rejected(self) -> None:
        """TC-SEC-03: Bundle with manifest modified after signing is rejected."""
        app_dir = self._create_bundle("tampered-app", sign=True)
        manifest_path = os.path.join(app_dir, "manifest.json")
        # Tamper manifest after signing
        with open(manifest_path, "r", encoding="utf-8") as fh:
            manifest = json.load(fh)
        manifest["name"] = "Tampered Name"
        with open(manifest_path, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=2)

        ok, msg, entry = self.service.install_app(manifest_path, app_root=app_dir)
        self.assertFalse(ok)
        self.assertIn("signature verification failed", msg)

    def test_sec_04_tampered_signature_rejected(self) -> None:
        """TC-SEC-04: Bundle with invalid/corrupted signature bytes is rejected."""
        app_dir = self._create_bundle("corrupt-sig-app", sign=True, tamper=True)
        manifest_path = os.path.join(app_dir, "manifest.json")
        ok, msg, entry = self.service.install_app(manifest_path, app_root=app_dir)
        self.assertFalse(ok)
        self.assertIn("signature verification failed", msg)

    def test_sec_05_unknown_key_id_rejected(self) -> None:
        """TC-SEC-05: Bundle signed with an unknown key_id is rejected."""
        app_dir = self._create_bundle("unknown-key-app", sign=True, key_id="untrusted-adversary-key")
        manifest_path = os.path.join(app_dir, "manifest.json")
        ok, msg, entry = self.service.install_app(manifest_path, app_root=app_dir)
        self.assertFalse(ok)
        self.assertIn("unknown key_id", msg)

    def test_sec_06_signature_path_traversal_rejected(self) -> None:
        """TC-SEC-06: Manifest pointing signature outside bundle root is rejected."""
        app_dir = self._create_bundle("traversal-sig-app", sign=True)
        manifest_path = os.path.join(app_dir, "manifest.json")
        with open(manifest_path, "r", encoding="utf-8") as fh:
            manifest = json.load(fh)
        manifest["release"]["signature"] = "../outside.sig"
        with open(manifest_path, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=2)

        ok, msg, entry = self.service.install_app(manifest_path, app_root=app_dir)
        self.assertFalse(ok)
        self.assertIn("escapes app dir", msg)

    def test_sec_07_token_stored_as_salted_hash(self) -> None:
        """TC-SEC-07: Tokens on disk must be stored as salted SHA-256 hashes ($ separated)."""
        app_dir = self._create_bundle("token-hash-app", sign=True)
        manifest_path = os.path.join(app_dir, "manifest.json")
        ok, msg, entry = self.service.install_app(manifest_path, app_root=app_dir)
        self.assertTrue(ok)
        raw_token = entry["token"]

        # Read stored record directly
        stored = read_token(self.data_dir, "token-hash-app")
        self.assertIsNotNone(stored)
        self.assertIn("$", stored)
        salt, token_hash = stored.split("$", 1)
        self.assertEqual(len(salt), 32)
        self.assertEqual(len(token_hash), 64)
        # Verify that stored string does NOT equal raw token
        self.assertNotEqual(stored, raw_token)
        # Verify verification works against hash
        self.assertTrue(verify_token(self.data_dir, "token-hash-app", raw_token))
        self.assertFalse(verify_token(self.data_dir, "token-hash-app", "wrong-token"))

    def test_sec_08_token_rotation(self) -> None:
        """TC-SEC-08: Token rotation invalidates previous token and activates new token."""
        app_dir = self._create_bundle("token-rot-app", sign=True)
        manifest_path = os.path.join(app_dir, "manifest.json")
        ok, msg, entry = self.service.install_app(manifest_path, app_root=app_dir)
        self.assertTrue(ok)
        old_token = entry["token"]

        # Rotate token
        new_token = rotate_token(self.data_dir, "token-rot-app")
        self.assertNotEqual(old_token, new_token)

        # Old token rejected, new token accepted
        self.assertFalse(verify_token(self.data_dir, "token-rot-app", old_token))
        self.assertTrue(verify_token(self.data_dir, "token-rot-app", new_token))

    def test_sec_09_cross_app_token_isolation(self) -> None:
        """TC-SEC-09: Token issued for App A cannot be used to authenticate for App B."""
        app_dir_a = self._create_bundle("app-alpha", sign=True)
        app_dir_b = self._create_bundle("app-beta", sign=True)

        ok_a, _, entry_a = self.service.install_app(os.path.join(app_dir_a, "manifest.json"), app_root=app_dir_a)
        ok_b, _, entry_b = self.service.install_app(os.path.join(app_dir_b, "manifest.json"), app_root=app_dir_b)
        self.assertTrue(ok_a)
        self.assertTrue(ok_b)

        token_a = entry_a["token"]
        token_b = entry_b["token"]

        # Each token verifies only for its own app
        self.assertTrue(verify_token(self.data_dir, "app-alpha", token_a))
        self.assertFalse(verify_token(self.data_dir, "app-alpha", token_b))
        self.assertTrue(verify_token(self.data_dir, "app-beta", token_b))
        self.assertFalse(verify_token(self.data_dir, "app-beta", token_a))


if __name__ == "__main__":
    unittest.main()
