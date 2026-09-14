"""Manifest validation (V2.1 proposal 4.3 / I2 + 2.6).

Malformed or incomplete manifests are REJECTED at install (+ audit event).

Rejection rules implemented here (map to proposal clauses):
  - required fields present (4.3): manifest_version, app_id, name,
    version, capabilities, models (>=1), adapter, release,
    input_schema, output_schema, resource_limits, api.authentication
  - manifest_version == 1 (F6)
  - app_id slug [a-z0-9-], max 32, not empty (O2)
  - version semver-ish (O2)
  - capabilities known (O3, fail-closed)
  - models: >=1, each {role, file, format, sha256}; format in E1
    whitelist; depends_on optional
  - adapter REQUIRED (R5): entry + files [{file, sha256}]
  - release REQUIRED (R6): key_id + signature present
  - api.authentication in {"token","none"} (R8); default "token"
  - resource_limits: max_memory, max_cpu, inference_timeout,
    concurrency present
  - permissions default no grants (O4)
  - input_schema/output_schema are JSON objects

Security-test cases (GeneralTask Day 1) are exercised in tests/:
  missing manifest, invalid JSON, invalid app_id, unknown capability,
  missing adapter, missing model, incorrect SHA-256, undeclared model
  artifact, path traversal, unsupported model format.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from .manifest import (
    APP_ID_MAX_LEN,
    APP_ID_PATTERN,
    KNOWN_CAPABILITIES,
    MANIFEST_VERSION_CURRENT,
    MODEL_FORMAT_WHITELIST,
    REQUIRED_MANIFEST_FIELDS,
)
from .network_policy import ALLOWED_NETWORK_POLICIES

SEMVER_PATTERN = re.compile(r"^\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?(\+[0-9A-Za-z.-]+)?$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def validate_manifest(manifest: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """Validate a parsed manifest.

    Returns (ok, errors). Errors is a list of human-readable rejection
    reasons. NEVER raises; malformed input yields (False, reasons).
    """
    errors: List[str] = []

    if not isinstance(manifest, dict):
        return False, ["manifest must be a JSON object"]

    # Required top-level fields (4.3)
    for field in REQUIRED_MANIFEST_FIELDS:
        if field not in manifest:
            errors.append(f"missing required field: {field}")

    # manifest_version (F6)
    if "manifest_version" in manifest:
        mv = manifest["manifest_version"]
        if not isinstance(mv, int) or isinstance(mv, bool):
            errors.append("manifest_version must be an integer")
        elif mv != MANIFEST_VERSION_CURRENT:
            errors.append(
                f"unsupported manifest_version {mv}; expected {MANIFEST_VERSION_CURRENT}"
            )

    # app_id (O2)
    if "app_id" in manifest:
        app_id = manifest["app_id"]
        if not isinstance(app_id, str):
            errors.append("app_id must be a string")
        else:
            if len(app_id) == 0:
                errors.append("app_id must not be empty")
            if len(app_id) > APP_ID_MAX_LEN:
                errors.append(
                    f"app_id too long ({len(app_id)} > {APP_ID_MAX_LEN})"
                )
            if not re.fullmatch(APP_ID_PATTERN, app_id):
                errors.append(
                    "app_id must match slug pattern [a-z0-9-] (lowercase only)"
                )

    # name
    if "name" in manifest and not isinstance(manifest["name"], str):
        errors.append("name must be a string")

    # version (O2, semver)
    if "version" in manifest:
        version = manifest["version"]
        if not isinstance(version, str) or not SEMVER_PATTERN.fullmatch(version):
            errors.append(f"version must be semver, got: {version!r}")

    # capabilities (O3, fail-closed)
    if "capabilities" in manifest:
        caps = manifest["capabilities"]
        if not isinstance(caps, list) or not caps:
            errors.append("capabilities must be a non-empty list")
        else:
            for cap in caps:
                if not isinstance(cap, str) or cap not in KNOWN_CAPABILITIES:
                    errors.append(f"unknown capability: {cap!r}")
            if "inference" not in caps:
                errors.append("capabilities must include 'inference'")

    # models (E3/E5/E1)
    if "models" in manifest:
        models = manifest["models"]
        if not isinstance(models, list) or not models:
            errors.append("models must be a non-empty list")
        else:
            seen_roles = set()
            for i, model in enumerate(models):
                if not isinstance(model, dict):
                    errors.append(f"models[{i}] must be an object")
                    continue
                for key in ("role", "file", "format", "sha256"):
                    if key not in model:
                        errors.append(f"models[{i}] missing required key: {key}")
                role = model.get("role")
                if not isinstance(role, str) or not role:
                    errors.append(f"models[{i}].role must be a non-empty string")
                elif role in seen_roles:
                    errors.append(f"models[{i}].role duplicated: {role}")
                seen_roles.add(role)
                fmt = model.get("format")
                if fmt not in MODEL_FORMAT_WHITELIST:
                    errors.append(
                        f"models[{i}].format {fmt!r} not in E1 whitelist "
                        f"{sorted(MODEL_FORMAT_WHITELIST)}"
                    )
                sha = model.get("sha256")
                if not isinstance(sha, str) or not SHA256_PATTERN.fullmatch(sha):
                    errors.append(f"models[{i}].sha256 must be a 64-hex sha256")
                file_ = model.get("file")
                if not isinstance(file_, str) or not file_:
                    errors.append(f"models[{i}].file must be a non-empty string")

    # adapter (R5)
    if "adapter" in manifest:
        adapter = manifest["adapter"]
        if not isinstance(adapter, dict):
            errors.append("adapter must be an object")
        else:
            if not isinstance(adapter.get("entry"), str) or not adapter["entry"]:
                errors.append("adapter.entry must be a non-empty string")
            files = adapter.get("files")
            if not isinstance(files, list) or not files:
                errors.append("adapter.files must be a non-empty list")
            else:
                for i, f in enumerate(files):
                    if not isinstance(f, dict):
                        errors.append(f"adapter.files[{i}] must be an object")
                        continue
                    if not isinstance(f.get("file"), str) or not f["file"]:
                        errors.append(
                            f"adapter.files[{i}].file must be a non-empty string"
                        )
                    sha = f.get("sha256")
                    if not isinstance(sha, str) or not SHA256_PATTERN.fullmatch(sha):
                        errors.append(
                            f"adapter.files[{i}].sha256 must be a 64-hex sha256"
                        )

    # release (R6)
    if "release" in manifest:
        release = manifest["release"]
        if not isinstance(release, dict):
            errors.append("release must be an object")
        else:
            if not isinstance(release.get("key_id"), str) or not release["key_id"]:
                errors.append("release.key_id must be a non-empty string")
            if not isinstance(release.get("signature"), str) or not release["signature"]:
                errors.append("release.signature must be a non-empty string")

    # api.authentication (R8)
    if "api" in manifest:
        api = manifest["api"]
        if not isinstance(api, dict):
            errors.append("api must be an object")
        elif "authentication" in api:
            if api["authentication"] not in ("token", "none"):
                errors.append(
                    "api.authentication must be 'token' or 'none'"
                )

    # resource_limits (A3)
    if "resource_limits" in manifest:
        rl = manifest["resource_limits"]
        if not isinstance(rl, dict):
            errors.append("resource_limits must be an object")
        else:
            for key in ("max_memory", "max_cpu", "inference_timeout", "concurrency"):
                if key not in rl:
                    errors.append(f"resource_limits missing: {key}")
                elif not isinstance(rl[key], (int, float)) or isinstance(rl[key], bool):
                    errors.append(f"resource_limits.{key} must be a number")

    # input_schema / output_schema (H1)
    for key in ("input_schema", "output_schema"):
        if key in manifest and not isinstance(manifest[key], dict):
            errors.append(f"{key} must be a JSON Schema object")

    # permissions (O4): fail closed; network policy must be explicit
    if "permissions" not in manifest:
        errors.append("permissions missing: network policy must be explicit")
    elif not isinstance(manifest["permissions"], dict):
        errors.append("permissions must be an object")
    else:
        network = manifest["permissions"].get("network")
        if network is None:
            errors.append("permissions.network missing")
        elif network not in ALLOWED_NETWORK_POLICIES:
            errors.append(
                "permissions.network must be one of "
                f"{sorted(ALLOWED_NETWORK_POLICIES)}, got: {network!r}"
            )

    # dependencies (J2/J4): if present, list of {name, version}
    if "dependencies" in manifest:
        deps = manifest["dependencies"]
        if not isinstance(deps, list):
            errors.append("dependencies must be a list")
        else:
            for i, dep in enumerate(deps):
                if not isinstance(dep, dict):
                    errors.append(f"dependencies[{i}] must be an object")
                elif not isinstance(dep.get("name"), str) or not isinstance(
                    dep.get("version"), str
                ):
                    errors.append(
                        f"dependencies[{i}] must have name and version strings"
                    )

    return (len(errors) == 0, errors)
