"""Registry (V2.1 proposal 2.8 / F1).

Core-owned directory registry, ONE JSON file per app
(/var/lib/sydeco-lightml/registry/apps/{app_id}.json) holding:
  app_id, versions, active version, manifest content, artifact hashes,
  status (installed/active/disabled), install timestamps, filesystem
  paths.

Development: local dev equivalent under SYDECO_LIGHTML_DATA_DIR
(GS_LIGHTML_DATA_DIR or ./data). Path = <data_dir>/registry/apps/{app_id}.json

Writes are atomic (temp file + rename); ONLY Core writes. Every registry
change produces an audit event (C2). No SQLite.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


def default_data_dir() -> str:
    env = os.environ.get("SYDECO_LIGHTML_DATA_DIR")
    if env:
        return env
    # relative to this repo when no env set (dev default)
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "data"))


class Registry:
    """File-per-app registry with atomic writes."""

    def __init__(self, data_dir: Optional[str] = None, audit=None) -> None:
        self.data_dir = data_dir or default_data_dir()
        self.apps_dir = os.path.join(self.data_dir, "registry", "apps")
        os.makedirs(self.apps_dir, exist_ok=True)
        self.audit = audit

    def _app_path(self, app_id: str) -> str:
        # app_id is validated slug [a-z0-9-]; still guard against traversal
        if not app_id or "/" in app_id or "\\" in app_id or app_id in (".", ".."):
            raise ValueError(f"invalid app_id: {app_id!r}")
        return os.path.join(self.apps_dir, f"{app_id}.json")

    def _emit_audit(self, action: str, app_id: str, result: str, **extra: Any) -> None:
        if self.audit is not None:
            event = {
                "action": action,
                "app_id": app_id,
                "result": result,
            }
            event.update(extra)
            self.audit.append(event)

    def _atomic_write(self, path: str, data: Dict[str, Any]) -> None:
        """F1: atomic write (temp file + rename)."""
        d = os.path.dirname(path)
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, sort_keys=True)
            os.replace(tmp, path)  # atomic rename
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def register(
        self,
        app_id: str,
        version: str,
        manifest: Dict[str, Any],
        artifact_hashes: Dict[str, str],
        filesystem_paths: Dict[str, str],
    ) -> Dict[str, Any]:
        """Register (or add version to) an app. Returns the registry entry.

        Called ONLY by Core (install flow). Emits audit event.
        """
        path = self._app_path(app_id)
        existing: Dict[str, Any] = {}
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                existing = json.load(fh)

        entry = existing if existing else {
            "app_id": app_id,
            "registry_schema": 1,  # F6
            "versions": {},
            "active_version": None,
            "status": "installed",
            "installed_at": None,
            "paths": {},
        }
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        entry["versions"][version] = {
            "version": version,
            "manifest": manifest,
            "artifact_hashes": artifact_hashes,
            "filesystem_paths": filesystem_paths,
            "installed_at": now,
        }
        entry["active_version"] = version
        entry["status"] = "active"
        entry["installed_at"] = now
        entry["paths"] = filesystem_paths

        self._atomic_write(path, entry)
        self._emit_audit(
            "install", app_id, "ok", version=version, registry_path=path
        )
        return entry

    def get(self, app_id: str) -> Optional[Dict[str, Any]]:
        """Read one app's registry entry (or None)."""
        path = self._app_path(app_id)
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)

    def list_apps(self) -> List[Dict[str, Any]]:
        """List all registered apps (summary)."""
        out: List[Dict[str, Any]] = []
        if not os.path.isdir(self.apps_dir):
            return out
        for fname in sorted(os.listdir(self.apps_dir)):
            if fname.endswith(".json"):
                try:
                    with open(
                        os.path.join(self.apps_dir, fname), "r", encoding="utf-8"
                    ) as fh:
                        entry = json.load(fh)
                    out.append(
                        {
                            "app_id": entry.get("app_id"),
                            "status": entry.get("status"),
                            "active_version": entry.get("active_version"),
                            "installed_at": entry.get("installed_at"),
                        }
                    )
                except (json.JSONDecodeError, OSError):
                    continue
        return out
