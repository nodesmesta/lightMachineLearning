"""Bundle-security suite (GeneralTask Day 3; proposal 3.4 / R6).

Six cases against the full install flow (CoreService.install_app):
  1. correctly signed bundle -> accepted (installs)
  2. unsigned bundle -> rejected
  3. modified manifest AFTER signing -> rejected
  4. modified Adapter AFTER signing -> rejected
  5. incorrect key -> rejected
  6. modified model AFTER signing -> rejected

Signature is verified FIRST (before validation/artifact checks); adapter
and model modifications are additionally caught by the E5/R5 artifact
sha256 verification. Every rejection emits an audit event.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from sydeco_lightml_core.core import CoreService

from tests._signing import _enable_test_trust, sign_manifest

ADAPTER_CODE = (
    "class Adapter:\n"
    "    def initialize(self, context):\n"
    "        pass\n"
    "    def infer(self, request, context):\n"
    "        return {'ok': True}\n"
    "    def shutdown(self):\n"
    "        pass\n"
)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _make_bundle(tmp: str, app_id: str = "sig-test-app") -> str:
    """Build a minimal valid bundle WITHOUT signing (caller controls)."""
    d = os.path.join(tmp, app_id)
    os.makedirs(os.path.join(d, "models"), exist_ok=True)
    os.makedirs(os.path.join(d, "adapter"), exist_ok=True)
    with open(os.path.join(d, "models", "model.pkl"), "wb") as fh:
        fh.write(b"sig-model-data")
    with open(os.path.join(d, "adapter", "main.py"), "w", encoding="utf-8") as fh:
        fh.write(ADAPTER_CODE)
    manifest = {
        "manifest_version": 1,
        "app_id": app_id,
        "name": "Signature Test App",
        "version": "1.0.0",
        "capabilities": ["inference"],
        "models": [
            {
                "role": "model",
                "file": "models/model.pkl",
                "format": "pickle",
                "sha256": _sha256(b"sig-model-data"),
            }
        ],
        "adapter": {
            "entry": "adapter/main.py",
            "files": [{"file": "adapter/main.py", "sha256": _sha256(ADAPTER_CODE.encode())}],
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
            "inference_timeout": 120,
            "concurrency": 1,
        },
        "dependencies": [],
    }
    with open(os.path.join(d, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)
    return d


class TestBundleSecurity(unittest.TestCase):
    def setUp(self) -> None:
        os.environ.pop("SYDECO_LIGHTML_TRUSTED_KEYS_FILE", None)
        self._tmp = tempfile.mkdtemp(prefix="sydeco-sig-")
        self.data_dir = os.path.join(self._tmp, "data")
        self.service = CoreService(data_dir=self.data_dir)

    def tearDown(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _install(self, bundle: str) -> tuple:
        return self.service.install_app(
            os.path.join(bundle, "manifest.json"), app_root=bundle
        )

    # 1. correctly signed bundle -> accepted
    def test_01_correctly_signed_accepted(self) -> None:
        bundle = _make_bundle(self._tmp)
        sign_manifest(bundle)
        ok, message, entry = self._install(bundle)
        self.assertTrue(ok, f"install failed: {message}")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["app_id"], "sig-test-app")

    # 2. unsigned bundle -> rejected
    def test_02_unsigned_rejected(self) -> None:
        bundle = _make_bundle(self._tmp)
        _enable_test_trust(bundle)
        ok, message, _ = self._install(bundle)
        self.assertFalse(ok, "unsigned bundle must be rejected (R6)")
        self.assertIn("unsigned bundle", message.lower())

    # 3. modified manifest AFTER signing -> rejected
    def test_03_modified_manifest_rejected(self) -> None:
        bundle = _make_bundle(self._tmp)
        sign_manifest(bundle)
        # tamper with the manifest AFTER signing (name changed)
        with open(os.path.join(bundle, "manifest.json"), "r", encoding="utf-8") as fh:
            manifest = json.load(fh)
        manifest["name"] = "Tampered Name"
        with open(os.path.join(bundle, "manifest.json"), "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=2)
        ok, message, _ = self._install(bundle)
        self.assertFalse(ok, "modified manifest must be rejected (R6)")
        self.assertIn("signature verification failed", message)

    # 4. modified Adapter AFTER signing -> rejected (E5/R5 artifact hash)
    def test_04_modified_adapter_rejected(self) -> None:
        bundle = _make_bundle(self._tmp)
        sign_manifest(bundle)
        with open(os.path.join(bundle, "adapter", "main.py"), "a", encoding="utf-8") as fh:
            fh.write("# tampered after signing\n")
        ok, message, _ = self._install(bundle)
        self.assertFalse(ok, "modified adapter must be rejected")
        self.assertIn("adapter sha256 mismatch", message)

    # 5. incorrect key -> rejected
    def test_05_incorrect_key_rejected(self) -> None:
        bundle = _make_bundle(self._tmp)
        # sign with a DIFFERENT key (not the trusted Core key)
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        other_key = Ed25519PrivateKey.generate()
        other_path = os.path.join(self._tmp, "other_key.pem")
        with open(other_path, "wb") as fh:
            fh.write(
                other_key.private_bytes(
                    encoding=serialization.Encoding.PEM,
                    format=serialization.PrivateFormat.PKCS8,
                    encryption_algorithm=serialization.NoEncryption(),
                )
            )
        sign_manifest(bundle)
        sign_manifest(bundle, key_path=other_path)
        ok, message, _ = self._install(bundle)
        self.assertFalse(ok, "bundle signed with an untrusted key must be rejected")
        self.assertIn("signature verification failed", message)

    # 6. modified model AFTER signing -> rejected (E5 artifact hash)
    def test_06_modified_model_rejected(self) -> None:
        bundle = _make_bundle(self._tmp)
        sign_manifest(bundle)
        with open(os.path.join(bundle, "models", "model.pkl"), "ab") as fh:
            fh.write(b"TAMPERED")
        ok, message, _ = self._install(bundle)
        self.assertFalse(ok, "modified model must be rejected")
        self.assertIn("sha256 mismatch", message)

    def test_07_test_key_is_not_trusted_by_default(self) -> None:
        bundle = _make_bundle(self._tmp)
        sign_manifest(bundle)

        with mock.patch.dict(os.environ, {}, clear=True):
            ok, message, _ = self._install(bundle)

        self.assertFalse(ok, "production default must not trust the dev/test key")
        self.assertIn("unknown key_id", message)

    def test_08_runtime_trust_file_accepts_public_key_without_private_key(self) -> None:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        bundle = _make_bundle(self._tmp, app_id="prod-signed-app")
        prod_key = Ed25519PrivateKey.generate()
        prod_key_path = os.path.join(self._tmp, "prod_private_not_in_runtime.pem")
        prod_private_pem = prod_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        with open(prod_key_path, "wb") as fh:
            fh.write(prod_private_pem)
        public_pem = prod_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode("ascii")
        trust_file = os.path.join(self._tmp, "trusted-public-keys.json")
        with open(trust_file, "w", encoding="utf-8") as fh:
            json.dump({"sydeco-production-rc2-v1": public_pem}, fh)

        with open(os.path.join(bundle, "manifest.json"), "r", encoding="utf-8") as fh:
            manifest = json.load(fh)
        manifest["release"]["key_id"] = "sydeco-production-rc2-v1"
        with open(os.path.join(bundle, "manifest.json"), "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=2)
        sign_manifest(bundle, key_path=prod_key_path)

        with mock.patch.dict(
            os.environ,
            {"SYDECO_LIGHTML_TRUSTED_KEYS_FILE": trust_file},
            clear=False,
        ):
            ok, message, entry = self._install(bundle)

        self.assertTrue(ok, f"production public trust file should verify: {message}")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["app_id"], "prod-signed-app")


if __name__ == "__main__":
    unittest.main()
