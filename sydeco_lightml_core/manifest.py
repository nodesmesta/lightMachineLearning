"""Manifest parser (V2.1 proposal 4.1 / B3 / I1-I3).

The application manifest is the ONE source of truth per version.
Fields (GeneralTask Day 1 list == proposal 4.1):

    manifest_version   integer, schema version (F6); currently 1
    app_id             slug [a-z0-9-], max 32 chars, unique (O2)
    name               human-readable name
    version            semver (O2)
    capabilities       list; inference required, ui optional (O3)
    models             array of artifacts: {role, file, format, sha256,
                       depends_on?} (E3); format from E1 whitelist
    adapter            REQUIRED (R5): {entry, files:[{file, sha256}]}
    release            REQUIRED (R6): {key_id, signature}
    input_schema       JSON Schema draft 2020-12 (H1)
    output_schema      JSON Schema draft 2020-12 (H1)
    permissions        optional; default no grants (O4)
    api                REQUIRED (R8): {authentication: "token"|"none"}
    resource_limits    REQUIRED (A3): max_memory, max_cpu,
                       inference_timeout, concurrency
    dependencies       optional (J2/J4)
    ui                 optional (K7)
    inference.seed     optional (M2)

Validation happens in validation.py (4.3/I2); this module only loads
JSON and exposes the field structure.
"""
from __future__ import annotations

import json
from typing import Any, Dict, Optional

# Supported model formats (E1 whitelist - baseline V2).
MODEL_FORMAT_WHITELIST = frozenset({"joblib", "pickle", "torch"})

# Capabilities known to Core (O3, fail-closed: unknown -> rejected).
KNOWN_CAPABILITIES = frozenset({"inference", "ui"})

# Required fields per proposal 4.3 (adapter is optional for zero-code models).
REQUIRED_MANIFEST_FIELDS = (
    "manifest_version",
    "app_id",
    "name",
    "version",
    "capabilities",
    "models",
    "release",
    "input_schema",
    "output_schema",
    "resource_limits",
    "api",
)

MANIFEST_VERSION_CURRENT = 1

APP_ID_PATTERN = r"^[a-z0-9-]+$"
APP_ID_MAX_LEN = 32


class ManifestError(Exception):
    """Raised when a manifest cannot be loaded or is structurally invalid."""


def load_manifest(path: str) -> Dict[str, Any]:
    """Load a manifest JSON file. Raises ManifestError on missing/invalid."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        raise ManifestError(f"manifest not found: {path}")
    except json.JSONDecodeError as exc:
        raise ManifestError(f"invalid JSON in manifest: {exc}")
    except OSError as exc:
        raise ManifestError(f"cannot read manifest: {exc}")


def load_manifest_str(text: str) -> Dict[str, Any]:
    """Load a manifest from a string (for tests)."""
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ManifestError(f"invalid JSON in manifest: {exc}")
