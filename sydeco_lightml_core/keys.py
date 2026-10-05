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
import hashlib
import json
import os
from typing import Any, Dict, Optional, Tuple

try:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
        Ed25519PublicKey,
    )
except ImportError:
    InvalidSignature = Exception  # type: ignore
    serialization = None  # type: ignore
    Ed25519PrivateKey = None  # type: ignore
    Ed25519PublicKey = None  # type: ignore

TRUSTED_KEYS_FILE_ENV = "SYDECO_LIGHTML_TRUSTED_KEYS_FILE"

# Standard development public verification key embedded as baseline trust anchor
DEFAULT_DEV_KEY_ID = "sydeco-test-key-v1"
DEFAULT_DEV_PUBKEY_PEM = (
    "-----BEGIN PUBLIC KEY-----\n"
    "MCowBQYDK2VwAyEAxBCfVdolI+t7Sbhq7v9VihmCaWUqS24l5c9Oy5K7eYA=\n"
    "-----END PUBLIC KEY-----\n"
)

# ---------------------------------------------------------------------------
# Pure-Python RFC 8032 Edwards25519 Engine (Air-Gapped Sovereign Fallback)
# ---------------------------------------------------------------------------
_ED25519_P = 2**255 - 19
_ED25519_D = -121665 * pow(121666, _ED25519_P - 2, _ED25519_P) % _ED25519_P
_ED25519_Q = 2**252 + 27742317777372353535851937790883648493
_ED25519_BX = 15112221349535400772501151409588531511454012693041857206046113283949847762202
_ED25519_BY = 46316835694926478169428394003475163141307993866256225615783033603165251855960
_ED25519_B = (_ED25519_BX, _ED25519_BY)


def _ed_point_add(P: Tuple[int, int], Q: Tuple[int, int]) -> Tuple[int, int]:
    (x1, y1), (x2, y2) = P, Q
    p, d = _ED25519_P, _ED25519_D
    x3 = (x1 * y2 + y1 * x2) * pow(1 + d * x1 * x2 * y1 * y2, p - 2, p) % p
    y3 = (y1 * y2 + x1 * x2) * pow(1 - d * x1 * x2 * y1 * y2, p - 2, p) % p
    return (x3, y3)


def _ed_point_mul(s: int, P: Tuple[int, int]) -> Tuple[int, int]:
    Q = (0, 1)
    while s > 0:
        if s & 1:
            Q = _ed_point_add(Q, P)
        P = _ed_point_add(P, P)
        s >>= 1
    return Q


def _ed_point_compress(P: Tuple[int, int]) -> bytes:
    x, y = P
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def _ed_recover_x(y: int, sign: int) -> Optional[int]:
    p, d = _ED25519_P, _ED25519_D
    if y >= p:
        return None
    x2 = (y * y - 1) * pow(d * y * y + 1, p - 2, p) % p
    if x2 == 0:
        return 0 if sign == 0 else None
    x = pow(x2, (p + 3) // 8, p)
    if (x * x - x2) % p != 0:
        x = (x * pow(2, (p - 1) // 4, p)) % p
    if (x * x - x2) % p != 0:
        return None
    if (x & 1) != sign:
        x = p - x
    return x


def _ed_point_decompress(s: bytes) -> Optional[Tuple[int, int]]:
    if len(s) != 32:
        return None
    y = int.from_bytes(s, "little")
    sign = (y >> 255) & 1
    y &= (1 << 255) - 1
    x = _ed_recover_x(y, sign)
    if x is None:
        return None
    return (x, y)


def _pure_ed25519_verify(msg: bytes, sig: bytes, pub: bytes) -> bool:
    if len(sig) != 64 or len(pub) != 32:
        return False
    R_bytes = sig[:32]
    S_bytes = sig[32:]
    S = int.from_bytes(S_bytes, "little")
    if S >= _ED25519_Q:
        return False
    A = _ed_point_decompress(pub)
    if A is None:
        return False
    R = _ed_point_decompress(R_bytes)
    if R is None:
        return False
    k = int.from_bytes(hashlib.sha512(R_bytes + pub + msg).digest(), "little")
    return _ed_point_mul(S, _ED25519_B) == _ed_point_add(R, _ed_point_mul(k, A))


def _pure_ed25519_sign(msg: bytes, seed: bytes) -> bytes:
    h = bytearray(hashlib.sha512(seed).digest())
    h[0] &= 248
    h[31] &= 127
    h[31] |= 64
    a = int.from_bytes(h[:32], "little")
    prefix = bytes(h[32:])
    A = _ed_point_mul(a, _ED25519_B)
    pub = _ed_point_compress(A)
    r = int.from_bytes(hashlib.sha512(prefix + msg).digest(), "little")
    R = _ed_point_mul(r, _ED25519_B)
    R_bytes = _ed_point_compress(R)
    k = int.from_bytes(hashlib.sha512(R_bytes + pub + msg).digest(), "little")
    S = (r + k * a) % _ED25519_Q
    return R_bytes + S.to_bytes(32, "little")


def _raw_pubkey_from_pem(pem_str: str) -> bytes:
    """Extract 32-byte Ed25519 raw public key from SubjectPublicKeyInfo PEM."""
    lines = [
        line.strip()
        for line in pem_str.strip().splitlines()
        if not line.startswith("-----")
    ]
    raw = base64.b64decode("".join(lines))
    if len(raw) == 32:
        return raw
    # Standard Ed25519 SPKI is 44 bytes with 12 bytes ASN.1 header (302a300506032b6570032100)
    if len(raw) == 44 and raw[:12].hex() == "302a300506032b6570032100":
        return raw[12:]
    raise ValueError("unrecognized Ed25519 public key format")


def _raw_privkey_from_pem(pem_str: str) -> bytes:
    """Extract 32-byte Ed25519 private seed from PKCS#8 PEM."""
    lines = [
        line.strip()
        for line in pem_str.strip().splitlines()
        if not line.startswith("-----")
    ]
    raw = base64.b64decode("".join(lines))
    if len(raw) == 32:
        return raw
    # Standard Ed25519 PKCS#8 is 48 bytes with 16 bytes ASN.1 header (302e020100300506032b657004220420)
    if len(raw) == 48 and raw[:16].hex() == "302e020100300506032b657004220420":
        return raw[16:]
    raise ValueError("unrecognized Ed25519 private key format")


# ---------------------------------------------------------------------------
# Canonical Manifest Serialization & Trust Store Resolution
# ---------------------------------------------------------------------------

def canonical_manifest_bytes(manifest: Dict[str, Any]) -> bytes:
    """Canonical serialization of the manifest (the R6 signed payload).

    sort_keys + minimal separators make the digest independent of JSON
    formatting whitespace; ANY semantic change to the manifest changes
    the digest and therefore invalidates the signature.
    """
    return json.dumps(
        manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _load_trusted_public_keys(
    data_dir: Optional[str] = None, allow_dev_fallback: bool = False
) -> Dict[str, str]:
    """Return trusted public verification keys by key_id.

    Resolution hierarchy:
    1. SYDECO_LIGHTML_TRUSTED_KEYS_FILE environment variable.
    2. /etc/sydeco-lightml/trusted_keys.json (system configuration).
    3. <data_dir>/trusted_keys.json (runtime data store).
    Fail-closed: returns empty dict if no trusted keys file is found.
    """
    trusted: Dict[str, str] = {}
    candidate_paths = []

    env_path = os.environ.get(TRUSTED_KEYS_FILE_ENV, "").strip()
    if env_path:
        candidate_paths.append(env_path)
    candidate_paths.append("/etc/sydeco-lightml/trusted_keys.json")
    if data_dir:
        candidate_paths.append(os.path.join(data_dir, "trusted_keys.json"))

    loaded_from_file = False
    for path in candidate_paths:
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            if not isinstance(raw, dict):
                raise ValueError(f"trusted keys file '{path}' must contain a JSON object")
            keys = raw.get("keys", raw)
            if not isinstance(keys, dict):
                raise ValueError(f"trusted keys file '{path}' 'keys' must be a JSON object")
            for key_id, pem in keys.items():
                if not isinstance(key_id, str) or not key_id:
                    raise ValueError("trusted key_id must be a non-empty string")
                if not isinstance(pem, str) or "PRIVATE KEY" in pem:
                    raise ValueError(f"trusted key {key_id!r} must be a public PEM string")
                trusted[key_id] = pem
            loaded_from_file = True
            break

    if not loaded_from_file and not trusted and allow_dev_fallback:
        trusted[DEFAULT_DEV_KEY_ID] = DEFAULT_DEV_PUBKEY_PEM

    return trusted


def sign_manifest_canonical(
    manifest: Dict[str, Any], private_key_pem_or_bytes: str | bytes
) -> str:
    """Sign canonical manifest bytes with Ed25519 private key.

    Returns base64 encoded signature string.
    """
    payload = canonical_manifest_bytes(manifest)
    pem_str = (
        private_key_pem_or_bytes.decode("ascii")
        if isinstance(private_key_pem_or_bytes, bytes)
        else private_key_pem_or_bytes
    )

    if serialization is not None and Ed25519PrivateKey is not None:
        try:
            priv_key = serialization.load_pem_private_key(
                pem_str.encode("ascii"), password=None
            )
            sig_bytes = priv_key.sign(payload)
            return base64.b64encode(sig_bytes).decode("ascii")
        except Exception:
            pass

    # Pure Python fallback
    seed = _raw_privkey_from_pem(pem_str)
    sig_bytes = _pure_ed25519_sign(payload, seed)
    return base64.b64encode(sig_bytes).decode("ascii")


def verify_bundle_signature(
    manifest: Dict[str, Any], signature_path: str, data_dir: Optional[str] = None
) -> Tuple[bool, str]:
    """Verify release.signature (manifest.sig) over the canonical manifest.

    Returns (ok, reason). Fail-closed: unknown key_id, missing/unreadable
    signature file, malformed base64 or a bad signature all yield
    (False, reason) -> install refused (R6) + audit.
    """
    release = manifest.get("release", {})
    key_id = release.get("key_id", "")
    try:
        trusted_keys = _load_trusted_public_keys(data_dir=data_dir)
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

    payload = canonical_manifest_bytes(manifest)

    # 1. Try C-accelerated cryptography package if available
    if serialization is not None and Ed25519PublicKey is not None:
        try:
            public_key = serialization.load_pem_public_key(public_key_pem.encode("ascii"))
            if not isinstance(public_key, Ed25519PublicKey):
                return False, "trusted key is not an Ed25519 key"
            public_key.verify(signature, payload)
            return True, ""
        except InvalidSignature:
            return False, "signature verification failed (R6)"
        except Exception as exc:
            return False, f"signature verification error: {exc}"

    # 2. Pure-Python RFC 8032 fallback for air-gapped / minimal environments
    try:
        raw_pub = _raw_pubkey_from_pem(public_key_pem)
        if _pure_ed25519_verify(payload, signature, raw_pub):
            return True, ""
        return False, "signature verification failed (R6)"
    except Exception as exc:
        return False, f"signature verification error: {exc}"
