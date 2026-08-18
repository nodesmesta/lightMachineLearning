"""Worker manager (V2.1 proposal 2.3 / A3 - skeleton).

Per-capability process boundary; a Core supervisor starts, monitors and
restarts capability workers. Each worker runs its application's Adapter
under the stable contract (3.0). Resource limits (cgroup) and systemd
unit generation are ENFORCEMENT detail that belongs to the full worker
hosting layer (later days); Day 1 keeps the manager skeleton + contract
so the structure is in place.

Shutdown is bounded: graceful adapter.shutdown() with default 30 s
grace (from resource_limits), then SIGKILL.
"""
from __future__ import annotations

import abc
from typing import Any, Dict, Optional

from .adapter import Adapter
from .health import AppStatus, ReadinessStore


class WorkerHost(abc.ABC):
    """Abstract worker host: start / stop / restart one capability worker."""

    @abc.abstractmethod
    def start(self, app_id: str, version: str, adapter: Adapter, context: Dict[str, Any]) -> None:
        raise NotImplementedError

    @abc.abstractmethod
    def stop(self, app_id: str) -> None:
        raise NotImplementedError

    @abc.abstractmethod
    def restart(self, app_id: str) -> None:
        raise NotImplementedError

    @abc.abstractmethod
    def status(self, app_id: str) -> str:
        raise NotImplementedError


class WorkerManager:
    """Supervisor skeleton: tracks workers, drives readiness.

    Day 1: in-process registry of worker handles. The actual subprocess
    isolation (A3) is the hosting layer implemented on later days.
    """

    def __init__(self, readiness: Optional[ReadinessStore] = None) -> None:
        self.readiness = readiness or ReadinessStore()
        self._workers: Dict[str, WorkerHost] = {}

    def register_host(self, app_id: str, host: WorkerHost) -> None:
        self._workers[app_id] = host

    def start(self, app_id: str, version: str, adapter: Adapter, context: Dict[str, Any]) -> None:
        if app_id not in self._workers:
            raise KeyError(f"no worker host registered for {app_id}")
        self.readiness.set(app_id, AppStatus.LOADING)
        try:
            self._workers[app_id].start(app_id, version, adapter, context)
            self.readiness.set(app_id, AppStatus.READY)
        except Exception:
            self.readiness.set(app_id, AppStatus.BACKOFF, detail="start failed")
            raise

    def stop(self, app_id: str) -> None:
        if app_id in self._workers:
            self._workers[app_id].stop(app_id)
            self.readiness.set(app_id, AppStatus.IDLE)

    def restart(self, app_id: str) -> None:
        if app_id not in self._workers:
            raise KeyError(f"no worker host registered for {app_id}")
        self.stop(app_id)
        self.readiness.set(app_id, AppStatus.LOADING)
        try:
            self._workers[app_id].restart(app_id)
            self.readiness.set(app_id, AppStatus.READY)
        except Exception:
            self.readiness.set(app_id, AppStatus.BACKOFF, detail="restart failed")
            raise
