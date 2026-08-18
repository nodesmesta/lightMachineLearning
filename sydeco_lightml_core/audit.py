"""Audit interface (V2.1 proposal 2.10, C2 / D1-D5).

Storage: JSONL append-only in a Core-owned directory
(e.g. /var/log/sydeco-lightml/audit/), size-based rotation + retention.

Event set (D2): install, update, uninstall; inference per request
(status from the B1 taxonomy + duration_ms); readiness transitions;
resource-limit violations; auth failures; orphan artifact flags;
worker restarts; registry changes.

Each entry: ISO8601 timestamp, app_id, action, result/status,
request_id (when present), duration_ms (when present).

Payload/redaction (D3, decided (a)): NEVER record input payloads.
Tamper-evidence (D4): hash chain - each entry carries the hash of the
previous entry.
Access (D5): CLI admin read-only; capabilities have no write access.
"""
from __future__ import annotations

import abc
import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Any, Optional


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class AuditBackend(abc.ABC):
    """Abstract audit backend. Core writes events; capabilities cannot."""

    @abc.abstractmethod
    def append(self, event: dict[str, Any]) -> None:
        """Append one audit event (atomic-ish, append-only)."""
        raise NotImplementedError


class JsonlAuditBackend(AuditBackend):
    """Append-only JSONL backend with D4 hash chain.

    Development implementation: writes to a directory under the local
    data dir (SYDECO_LIGHTML_DATA_DIR/audit/). In production this maps to
    /var/log/sydeco-lightml/audit/.
    """

    def __init__(self, audit_dir: str) -> None:
        self.audit_dir = audit_dir
        os.makedirs(audit_dir, exist_ok=True)
        self._chain_hash = self._load_last_hash()

    def _load_last_hash(self) -> str:
        last = ""
        entries = sorted(
            p for p in os.listdir(self.audit_dir) if p.endswith(".jsonl")
        )
        if entries:
            with open(
                os.path.join(self.audit_dir, entries[-1]), "r", encoding="utf-8"
            ) as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        try:
                            last = json.loads(line).get("chain_prev_hash", "")
                        except json.JSONDecodeError:
                            last = ""
        return last

    def append(self, event: dict[str, Any]) -> None:
        event = dict(event)
        event.setdefault("timestamp", _now_iso())
        event.setdefault("chain_prev_hash", self._chain_hash)
        payload = json.dumps(event, sort_keys=True)
        self._chain_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        event["chain_hash"] = self._chain_hash
        line = json.dumps(event, sort_keys=True) + "\n"
        # Append-only: open in append mode; no in-place edits possible.
        with open(
            os.path.join(self.audit_dir, "audit.jsonl"), "a", encoding="utf-8"
        ) as fh:
            fh.write(line)
