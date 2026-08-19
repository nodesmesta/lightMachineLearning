"""Internal JSON Schema subset validator (V2.1 proposal 2.6 / B2 / H1).

The manifest's input_schema / output_schema fields are JSON Schema
(draft 2020-12). Core renders automatic validation from them — no
bespoke schema language (H1).

The dev environment is stdlib-only (jsonschema is NOT installed), so this
module implements the subset actually used by the Day-2 PoCs (and the
V2.1 reference examples):

  - type: object / string / number / integer / boolean / array / null
  - required (object), properties (object, recursive), items (array)
  - enum
  - minLength / maxLength (string)
  - minimum / maximum (number)
  - contentEncoding: "base64" (value must be valid base64)
  - minItems / maxItems (array)

Unknown keywords are ignored (forward compatibility); structural misuse
of a KNOWN keyword (e.g. minLength on a non-string schema) is tolerated
lazily — validation applies per value type, like a real validator.

Returns a list of human-readable error strings; never raises on data.
"""
from __future__ import annotations

import base64
import binascii
from typing import Any, Dict, List


def validate_schema(value: Any, schema: Dict[str, Any], path: str = "$") -> List[str]:
    """Validate `value` against `schema`. Returns list of error strings."""
    errors: List[str] = []
    _validate(value, schema, path, errors)
    return errors


def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _matches_type(value: Any, type_name: str) -> bool:
    if type_name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if type_name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    return _type_name(value) == type_name


def _validate(value: Any, schema: Dict[str, Any], path: str, errors: List[str]) -> None:
    if not isinstance(schema, dict):
        errors.append(f"{path}: schema must be an object")
        return

    # enum
    if "enum" in schema:
        if not isinstance(schema["enum"], list) or value not in schema["enum"]:
            errors.append(
                f"{path}: value not in enum {schema.get('enum')!r}"
            )
            # enum is a hard constraint; keep going for other checks anyway

    # type
    type_names = schema.get("type")
    if type_names is not None:
        allowed = type_names if isinstance(type_names, list) else [type_names]
        if not any(_matches_type(value, t) for t in allowed):
            errors.append(
                f"{path}: expected type {allowed!r}, got {_type_name(value)!r}"
            )
            return  # type mismatch: deeper checks are meaningless

    # object
    if isinstance(value, dict):
        for req in schema.get("required", []):
            if req not in value:
                errors.append(f"{path}: missing required field {req!r}")
        props = schema.get("properties", {})
        if isinstance(props, dict):
            for key, sub in props.items():
                if key in value:
                    _validate(value[key], sub, f"{path}.{key}", errors)
        return

    # array
    if isinstance(value, list):
        items = schema.get("items")
        if isinstance(items, dict):
            for i, item in enumerate(value):
                _validate(item, items, f"{path}[{i}]", errors)
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(f"{path}: fewer than {schema['minItems']} items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{path}: more than {schema['maxItems']} items")
        return

    # string
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append(
                f"{path}: shorter than minLength {schema['minLength']}"
            )
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(
                f"{path}: longer than maxLength {schema['maxLength']}"
            )
        enc = schema.get("contentEncoding")
        if enc == "base64":
            try:
                base64.b64decode(value, validate=True)
            except (binascii.Error, ValueError):
                errors.append(f"{path}: invalid base64 content")
        return

    # number / integer
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(
                f"{path}: less than minimum {schema['minimum']}"
            )
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(
                f"{path}: greater than maximum {schema['maximum']}"
            )
