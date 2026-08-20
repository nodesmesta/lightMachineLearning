"""Bundle signature verification (V2.1 proposal 3.4 / R6) — Day 3.

Signed SYDECO bundles are MANDATORY (R6): unsigned or incorrectly
signed capability -> installation refused + audit event. Signature is
verified FIRST, before any bundle code or data is extracted (3.1 step 1
/ 3.4).

This module embeds the TRUSTED public key inside Core (R6: "trusted key
embedded in Core") with a key_id in the signature for future rotation.

TEST-ONLY key. The label is mandated by the assignment:
    DEVELOPMENT_TEST_KEY_DO_NOT_USE_IN_PRODUCTION
The final SYDECO production signing key must never be created here; a
TEST/DEVELOPMENT key only may be used for implementation testing.
"""
from __future__ import annotations

import base64
import json
from typing import Any, Dict, Tuple

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

# R6 key_id for the development/test signing key (rotation-ready).
TEST_KEY_ID = "sydeco-test-key-v1"

# Trusted public key embedded in Core (R6). This is the PUBLIC half of
# the TEST keypair stored under devkeys/ in the dev repo; it MUST never
# be used for production bundles.
TRUSTED_PUBLIC_KEY_PEM = (
    "-----BEGIN PUBLIC KEY-----\n"
    "MCowBQYDK2VwAyEAxBCfVdolI+t7Sbhq7v9VihmCaWUqS24l5c9Oy5K7eYA=\n"
    "-----END PUBLIC KEY-----\n"
)


def canonical_manifest_bytes(manifest: Dict[str, Any]) -> bytes:
    """Canonical serialization of the manifest (the R6 signed payload).

    sort_keys + minimal separators make the digest independent of JSON
    formatting whitespace; ANY semantic change to the manifest changes
    the digest and therefore invalidates the signature.
    """
    return json.dumps(
        manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def verify_bundle_signature(
    manifest: Dict[str, Any], signature_path: str
) -> Tuple[bool, str]:
    """Verify release.signature (manifest.sig) over the canonical manifest.

    Returns (ok, reason). Fail-closed: unknown key_id, missing/unreadable
    signature file, malformed base64 or a bad signature all yield
    (False, reason) -> install refused (R6) + audit.
    """
    release = manifest.get("release", {})
    key_id = release.get("key_id", "")
    if key_id != TEST_KEY_ID:
        return False, f"unknown key_id: {key_id!r} (expected {TEST_KEY_ID!r})"

    try:
        with open(signature_path, "r", encoding="ascii") as fh:
            sig_b64 = fh.read().strip()
        if not sig_b64:
            return False, "empty signature file"
        signature = base64.b64decode(sig_b64)
    except OSError as exc:
        return False, f"unsigned bundle: cannot read signature file: {exc}"
    except Exception as exc:  # malformed base64 etc.
        return False, f"malformed signature file: {exc}"

    try:
        public_key = serialization.load_pem_public_key(
            TRUSTED_PUBLIC_KEY_PEM.encode("ascii")
        )
        if not isinstance(public_key, Ed25519PublicKey):
            return False, "trusted key is not an Ed25519 key"
        public_key.verify(signature, canonical_manifest_bytes(manifest))
        return True, ""
    except InvalidSignature:
        return False, "signature verification failed (R6)"
    except Exception as exc:
        return False, f"signature verification error: {exc}"
