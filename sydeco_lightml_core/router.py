"""Generic application routing (V2.1 proposal 2.11 / M1 - skeleton).

M1: the registry (B4) is the ROUTING TABLE: app_id -> active version ->
worker port + venv + models (E2). Core resolves the app_id from the
path (K1) -> checks target readiness (G2) -> edge validation (H3) ->
forwards to the worker -> the worker runs the generic handler
(load by role E3 -> adapter.infer (3.0) -> output validation H2) ->
returns. No per-type if-else anywhere.

Day 1: skeleton that resolves app_id -> active version from the registry
and delegates the rest to the hosting layer.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from .registry import Registry


class Router:
    """Resolves app_id -> active version using the registry (M1)."""

    def __init__(self, registry: Registry) -> None:
        self.registry = registry

    def resolve(self, app_id: str) -> Optional[Dict[str, Any]]:
        """Return the active-version routing info, or None if unknown."""
        entry = self.registry.get(app_id)
        if not entry:
            return None
        active = entry.get("active_version")
        if not active or active not in entry.get("versions", {}):
            return None
        version_info = entry["versions"][active]
        return {
            "app_id": app_id,
            "active_version": active,
            "manifest": version_info.get("manifest"),
            "artifact_hashes": version_info.get("artifact_hashes"),
            "filesystem_paths": version_info.get("filesystem_paths"),
            "status": entry.get("status"),
        }
