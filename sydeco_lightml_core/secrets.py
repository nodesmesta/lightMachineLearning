"""Per-app authentication tokens (V2.1 proposal 2.5 / K2 / R8, 2.9 / L1).

Every native V2 capability gets a Bearer token generated AT INSTALL
(K2/R8). Caller authentication (who may invoke the API) is independent of
network permissions.

Per proposal 2.9 (L1): Tokens are stored hashed (salted SHA-256, never
plaintext) in a Core-owned secrets directory with mode 0600, verified per
request at the edge (401 on missing/invalid).

Dev location: <data_dir>/secrets/<app_id>.token
"""
from __future__ import annotations

import hashlib
import os
import secrets
from typing import Optional

TOKEN_BYTES = 32  # -> 64 hex chars
SALT_BYTES = 16   # -> 32 hex chars


def generate_token() -> str:
    """Return a new random token (hex)."""
    return secrets.token_hex(TOKEN_BYTES)


def secrets_dir(data_dir: str) -> str:
    path = os.path.join(data_dir, "secrets")
    os.makedirs(path, exist_ok=True)
    return path


def _hash_token(raw_token: str, salt: str) -> str:
    return hashlib.sha256((salt + raw_token).encode("utf-8")).hexdigest()


def write_token(data_dir: str, app_id: str, token: str) -> str:
    """Persist a salted SHA-256 hashed token for app_id with mode 0600."""
    path = os.path.join(secrets_dir(data_dir), f"{app_id}.token")
    salt = secrets.token_hex(SALT_BYTES)
    token_hash = _hash_token(token, salt)
    content = f"{salt}${token_hash}\n"

    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
    except Exception:
        raise
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def read_token(data_dir: str, app_id: str) -> Optional[str]:
    """Return the raw stored record (<salt>$<hash> or legacy token), or None."""
    path = os.path.join(secrets_dir(data_dir), f"{app_id}.token")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read().strip()
    except (FileNotFoundError, OSError):
        return None


def verify_token(data_dir: str, app_id: str, provided: Optional[str]) -> bool:
    """Constant-time verification of provided Bearer token against stored hash."""
    if not provided:
        return False
    stored = read_token(data_dir, app_id)
    if stored is None:
        return False
    if "$" in stored:
        salt, expected_hash = stored.split("$", 1)
        calc_hash = _hash_token(provided, salt)
        return secrets.compare_digest(calc_hash, expected_hash)
    # Backward compatibility with legacy plaintext tokens
    return secrets.compare_digest(stored, provided)


def rotate_token(data_dir: str, app_id: str) -> str:
    """Generate and persist a new Bearer token for app_id, invalidating the old token.

    Returns the new raw token.
    """
    new_token = generate_token()
    write_token(data_dir, app_id, new_token)
    return new_token
