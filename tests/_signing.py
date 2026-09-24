"""Signing helper for tests (R6, bundle-security).

Signs a temp bundle's manifest with the dev TEST key
(DEVELOPMENT_TEST_KEY_DO_NOT_USE_IN_PRODUCTION) so the bundle passes the
install signature check (keys.py). The signed payload is the canonical
manifest JSON — identical to keys.canonical_manifest_bytes.
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

REPO_ROOT = Path(__file__).resolve().parents[1]
TEST_KEY_NAME = "DEVELOPMENT_TEST_KEY_DO_NOT_USE_IN_PRODUCTION"
TEST_PRIVATE_KEY = REPO_ROOT / "devkeys" / (TEST_KEY_NAME + ".pem")
TEST_PUBLIC_KEY = REPO_ROOT / "devkeys" / (TEST_KEY_NAME + ".pub.pem")
TEST_KEY_ID = "sydeco-test-key-v1"


def canonical_manifest_bytes(manifest: dict) -> bytes:
    return json.dumps(
        manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _load_private_key(key_path) -> Ed25519PrivateKey:
    with open(key_path, "rb") as fh:
        key = serialization.load_pem_private_key(fh.read(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise TypeError(f"not an Ed25519 private key: {key_path}")
    return key


def _enable_test_trust(bundle_dir: str) -> None:
    """Point runtime verification at a bundle-local explicit test trust file."""
    with open(TEST_PUBLIC_KEY, "r", encoding="ascii") as fh:
        public_pem = fh.read()
    trust_path = os.path.join(bundle_dir, ".test_trusted_public_keys.json")
    with open(trust_path, "w", encoding="utf-8") as fh:
        json.dump({TEST_KEY_ID: public_pem}, fh)
    os.environ["SYDECO_LIGHTML_TRUSTED_KEYS_FILE"] = trust_path


def sign_manifest(bundle_dir: str, key_path=None) -> str:
    """(Re)sign bundle_dir/manifest.json -> bundle_dir/manifest.sig.

    Returns the path of the written signature file. Default key is the
    repo TEST key; pass a different key_path to sign with another key
    (e.g. the 'incorrect key' bundle-security case).
    """
    if key_path is None:
        _enable_test_trust(bundle_dir)
    manifest_path = os.path.join(bundle_dir, "manifest.json")
    sig_path = os.path.join(bundle_dir, "manifest.sig")
    with open(manifest_path, "r", encoding="utf-8") as fh:
        manifest = json.load(fh)
    key = _load_private_key(key_path or TEST_PRIVATE_KEY)
    signature = key.sign(canonical_manifest_bytes(manifest))
    with open(sig_path, "w", encoding="ascii") as fh:
        fh.write(base64.b64encode(signature).decode("ascii") + "\n")
    return sig_path
