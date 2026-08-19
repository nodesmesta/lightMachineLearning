"""PoC B adapter — image classifier (V2.1 proposal 7.2 / R1-R4).

Images arrive base64-encoded per the input_schema (contentEncoding:
base64). This adapter decodes, parses the PGM (netpbm, P5 binary) image
with stdlib only, computes a brightness feature and classifies via the
joblib model params (E1 'joblib' artifact — plain dict data,
pickle-compatible). All application logic stays in the bundle (R2/R3).
"""
from __future__ import annotations

import base64
from typing import Any, Dict


class Adapter:
    """Prototype image classifier (brightness threshold on PGM images)."""

    def initialize(self, context: Dict[str, Any]) -> None:
        self._model = context["models"]["model"]  # {threshold, labels}
        self._threshold = float(self._model["threshold"])

    def infer(self, request: Dict[str, Any], context: Dict[str, Any]) -> Any:
        raw = base64.b64decode(request["image"])
        pixels = self._parse_pgm(raw)
        mean = sum(pixels) / len(pixels)
        label = "bright" if mean >= self._threshold else "dark"
        distance = abs(mean - self._threshold) / 255.0
        confidence = round(min(1.0, 0.5 + distance), 4)
        return {"label": label, "confidence": confidence}

    @staticmethod
    def _parse_pgm(data: bytes) -> list:
        """Parse a binary PGM (P5) image into a flat pixel list."""
        if not data.startswith(b"P5"):
            raise ValueError("unsupported image format (expected PGM P5)")
        pos = 2
        tokens = []
        while len(tokens) < 3:
            while pos < len(data) and data[pos : pos + 1].isspace():
                pos += 1
            start = pos
            while pos < len(data) and not data[pos : pos + 1].isspace():
                pos += 1
            if pos >= len(data):
                raise ValueError("truncated PGM header")
            tokens.append(data[start:pos])
        width, height, maxval = int(tokens[0]), int(tokens[1]), int(tokens[2])
        if maxval != 255:
            raise ValueError("unsupported PGM maxval (expected 255)")
        while pos < len(data) and data[pos : pos + 1].isspace():
            pos += 1  # single separating whitespace after maxval
        body = data[pos:]
        if len(body) != width * height:
            raise ValueError("PGM body size mismatch")
        return list(body)

    def shutdown(self) -> None:
        pass
