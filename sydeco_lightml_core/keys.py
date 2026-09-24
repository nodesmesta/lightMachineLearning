"""Bundle signature verification (V2.1 proposal 3.4 / R6).

Signed SYDECO bundles are MANDATORY (R6): unsigned or incorrectly
signed capability -> installation refused + audit event. Signature is
verified FIRST, before any bundle code or data is extracted (3.1 step 1
/ 3.4).

Production runtime trust is public-key only: a JSON trust store maps key_id to
PEM public verification keys. Private signing keys are never loaded here.
"""
from __future__ import annotations

import base64
import json
import os
from typing import Any, Dict, Tuple

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

TRUSTED_KEYS_FILE_ENV = "SYDECO_LIGHTML_TRUSTED_KEYS_FILE"


def canonical_manifest_bytes(manifest: Dict[str, Any]) -> bytes:
    """Canonical serialization of the manifest (the R6 signed payload).

    sort_keys + minimal separators make the digest independent of JSON
    formatting whitespace; ANY semantic change to the manifest changes
    the digest and therefore invalidates the signature.
    """
    return json.dumps(
        manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _load_trusted_public_keys() -> Dict[str, str]:
    """Return trusted public verification keys by key_id.

    Production deployments provide public keys through
    SYDECO_LIGHTML_TRUSTED_KEYS_FILE. The file may be either a direct
    {key_id: pem} object or {"keys": {key_id: pem}}.
    """
    trusted: Dict[str, str] = {}
    trust_path = os.environ.get(TRUSTED_KEYS_FILE_ENV, "").strip()
    if trust_path:
        with open(trust_path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        if not isinstance(raw, dict):
            raise ValueError("trusted keys file must contain a JSON object")
        keys = raw.get("keys", raw)
        if not isinstance(keys, dict):
            raise ValueError("trusted keys file 'keys' must be a JSON object")
        for key_id, pem in keys.items():
            if not isinstance(key_id, str) or not key_id:
                raise ValueError("trusted key_id must be a non-empty string")
            if not isinstance(pem, str) or "PRIVATE KEY" in pem:
                raise ValueError(f"trusted key {key_id!r} must be a public PEM string")
            trusted[key_id] = pem
    return trusted


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
    try:
        trusted_keys = _load_trusted_public_keys()
    except Exception as exc:
        return False, f"trusted key configuration error: {exc}"
    public_key_pem = trusted_keys.get(key_id)
    if not public_key_pem:
        return False, f"unknown key_id: {key_id!r}"

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
        public_key = serialization.load_pem_public_key(public_key_pem.encode("ascii"))
        if not isinstance(public_key, Ed25519PublicKey):
            return False, "trusted key is not an Ed25519 key"
        public_key.verify(signature, canonical_manifest_bytes(manifest))
        return True, ""
    except InvalidSignature:
        return False, "signature verification failed (R6)"
    except Exception as exc:
        return False, f"signature verification error: {exc}"
