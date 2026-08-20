#!/usr/bin/env python3
"""Sign a bundle's canonical manifest (R6) with the TEST key.

Usage:
    python3 tools/sign_bundle.py <bundle_dir> [--key <private_key.pem>]

Writes <bundle_dir>/manifest.sig: base64 Ed25519 signature over the
canonical manifest JSON (sort_keys, minimal separators — see
sydeco_lightml_core/keys.py::canonical_manifest_bytes). The default key
is the dev TEST key, clearly labelled:

    DEVELOPMENT_TEST_KEY_DO_NOT_USE_IN_PRODUCTION

Only a TEST/DEVELOPMENT signing key may be used (assignment rule); the
final SYDECO production signing key must never be created.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_KEY = os.path.join(
    REPO_ROOT,
    "devkeys",
    "DEVELOPMENT_TEST_KEY_DO_NOT_USE_IN_PRODUCTION.pem",
)

KEY_ID = "sydeco-test-key-v1"


def canonical_manifest_bytes(manifest: dict) -> bytes:
    return json.dumps(
        manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle_dir", help="bundle directory containing manifest.json")
    parser.add_argument(
        "--key", default=DEFAULT_KEY, help="path to the TEST private key (PEM)"
    )
    args = parser.parse_args(argv)

    manifest_path = os.path.join(args.bundle_dir, "manifest.json")
    with open(manifest_path, "r", encoding="utf-8") as fh:
        manifest = json.load(fh)

    manifest["release"]["key_id"] = KEY_ID
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)
        fh.write("\n")

    with open(args.key, "rb") as fh:
        key = serialization.load_pem_private_key(fh.read(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        print("error: key is not an Ed25519 private key", file=sys.stderr)
        return 1

    signature = key.sign(canonical_manifest_bytes(manifest))
    sig_path = os.path.join(args.bundle_dir, "manifest.sig")
    with open(sig_path, "w", encoding="ascii") as fh:
        fh.write(base64.b64encode(signature).decode("ascii") + "\n")

    print(f"signed: {manifest_path}")
    print(f"  key_id : {KEY_ID}")
    print(f"  key    : {os.path.basename(args.key)}")
    print(f"  output : {sig_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
