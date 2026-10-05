"""Signing helper for tests (R6, bundle-security).

Signs a temp bundle's manifest with the dev TEST key
(DEVELOPMENT_TEST_KEY_DO_NOT_USE_IN_PRODUCTION) so the bundle passes the
install signature check (keys.py). The signed payload is the canonical
manifest JSON — identical to keys.canonical_manifest_bytes.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from sydeco_lightml_core.keys import canonical_manifest_bytes, sign_manifest_canonical

REPO_ROOT = Path(__file__).resolve().parents[1]
TEST_KEY_NAME = "DEVELOPMENT_TEST_KEY_DO_NOT_USE_IN_PRODUCTION"
TEST_PRIVATE_KEY = REPO_ROOT / "devkeys" / (TEST_KEY_NAME + ".pem")
TEST_PUBLIC_KEY = REPO_ROOT / "devkeys" / (TEST_KEY_NAME + ".pub.pem")
TEST_KEY_ID = "sydeco-test-key-v1"


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
    with open(key_path or TEST_PRIVATE_KEY, "r", encoding="ascii") as fh:
        priv_pem = fh.read()
    sig_b64 = sign_manifest_canonical(manifest, priv_pem)
    with open(sig_path, "w", encoding="ascii") as fh:
        fh.write(sig_b64 + "\n")
    return sig_path
