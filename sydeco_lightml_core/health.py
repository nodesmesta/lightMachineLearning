"""Health / readiness (V2.1 proposal 2.2 / A2 / G1-G4).

G1: polling - Core polls each worker's /health/ready (default 5 s).
G2: per-capability, per-app gate - NOT global.
G4: /health, /health/live, /health/ready kept (1.0.1 contract), PLUS
    /health/apps -> per-app status (idle/loading/ready/backoff/disabled)
    and active version.

This module defines the status model and a readiness store; the HTTP
surface is implemented by the serving layer (Day 2+). Day 1 keeps the
structure (skeleton).
"""
from __future__ import annotations

import enum
import time
from typing import Any, Dict, Optional


class AppStatus(str, enum.Enum):
    IDLE = "idle"
    LOADING = "loading"
    READY = "ready"
    BACKOFF = "backoff"
    DISABLED = "disabled"


class ReadinessStore:
    """Per-app readiness state (G2: per-capability gate)."""

    def __init__(self) -> None:
        self._state: Dict[str, Dict[str, Any]] = {}

    def set(self, app_id: str, status: AppStatus, detail: str = "") -> None:
        self._state[app_id] = {
            "status": status.value,
            "detail": detail,
            "updated_at": time.time(),
        }

    def get(self, app_id: str) -> Optional[Dict[str, Any]]:
        return self._state.get(app_id)

    def status(self, app_id: str) -> str:
        entry = self._state.get(app_id)
        return entry["status"] if entry else AppStatus.IDLE.value

    def all(self) -> Dict[str, Dict[str, Any]]:
        return dict(self._state)

    def is_ready(self, app_id: str) -> bool:
        return self.status(app_id) == AppStatus.READY.value
