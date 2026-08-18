"""Dummy adapter for Day 1 (acceptance demo).

Implements the stable Core contract (3.0 / R1): initialize(context),
infer(request, context), shutdown(). This is application-specific logic
that lives in the APPLICATION BUNDLE (examples/dummy-app), never in Core.
"""
from __future__ import annotations

from typing import Any, Dict


class DummyAdapter:
    """Dummy adapter - proves registration without any Core modification."""

    def initialize(self, context: Dict[str, Any]) -> None:
        # Nothing real to prepare; a real adapter would use context.models.
        self._initialized = True

    def infer(self, request: Dict[str, Any], context: Dict[str, Any]) -> Any:
        # Echo-style dummy result matching the output_schema.
        return {
            "status": "ok",
            "app_id": context.get("config", {}).get("app_id", "dummy-app"),
            "echo": request.get("text", ""),
        }

    def shutdown(self) -> None:
        self._initialized = False
