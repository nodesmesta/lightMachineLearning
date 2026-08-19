"""Per-app authentication tokens (V2.1 proposal 2.5 / K2 / R8).

Every native V2 capability gets a Bearer token generated AT INSTALL
(K2/R8). Caller authentication (who may invoke the API) is independent of
network permissions. Tokens are per-app, stored in the Core-owned secrets
directory with mode 0600, verified per request at the edge (401 on
missing/invalid).

Dev location: <data_dir>/secrets/<app_id>.token
"""
from __future__ import annotations

import os
import secrets

TOKEN_BYTES = 32  # -> 64 hex chars


def generate_token() -> str:
    """Return a new random token (hex)."""
    return secrets.token_hex(TOKEN_BYTES)


def secrets_dir(data_dir: str) -> str:
    path = os.path.join(data_dir, "secrets")
    os.makedirs(path, exist_ok=True)
    return path


def write_token(data_dir: str, app_id: str, token: str) -> str:
    """Persist a token for app_id with mode 0600."""
    path = os.path.join(secrets_dir(data_dir), f"{app_id}.token")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(token + "\n")
    except Exception:
        raise
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def read_token(data_dir: str, app_id: str) -> str | None:
    """Return the stored token for app_id, or None."""
    path = os.path.join(secrets_dir(data_dir), f"{app_id}.token")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read().strip()
    except (FileNotFoundError, OSError):
        return None


def verify_token(data_dir: str, app_id: str, provided: str | None) -> bool:
    """Constant-time comparison of provided token vs stored token."""
    if not provided:
        return False
    stored = read_token(data_dir, app_id)
    if stored is None:
        return False
    return secrets.compare_digest(stored.encode(), provided.encode())
