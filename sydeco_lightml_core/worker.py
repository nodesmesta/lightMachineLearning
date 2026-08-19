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
import concurrent.futures
from concurrent.futures import ThreadPoolExecutor
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


class InferenceTimeout(Exception):
    """M4: the adapter did not return within inference_timeout."""


class WorkerNotReady(Exception):
    """The worker is broken/stopped and cannot serve this request."""


class InProcessWorkerHost(WorkerHost):
    """A3 dev equivalent (user decision D4, 2026-08-19): in-process worker
    host for the Day-2 PoCs.

    Production hosting = one subprocess per capability, managed by the
    Core supervisor with systemd/cgroup enforcement (proposal 2.3/A3,
    Section 3.3 — later days). This host runs the Adapter in-process,
    single-flight (M3, concurrency 1), with inference_timeout taken from
    the manifest resource_limits (M4 -> InferenceTimeout -> 504 at the
    serving layer).

    `simulate_crash()` is a DEV-ONLY test hook (isolation test P6): it
    marks the worker broken (readiness -> backoff) so the other app can
    be shown unaffected. Not part of any production path.
    """

    def __init__(
        self,
        readiness: Optional[ReadinessStore] = None,
        audit=None,
    ) -> None:
        self._app_id = ""
        self._version = ""
        self._adapter: Optional[Adapter] = None
        self._context: Dict[str, Any] = {}
        self._timeout = 120.0
        self._broken = False
        self._readiness = readiness
        self._audit = audit
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="sydeco-worker"
        )

    def start(
        self, app_id: str, version: str, adapter: Adapter, context: Dict[str, Any]
    ) -> None:
        self._app_id = app_id
        self._version = version
        self._adapter = adapter
        self._context = dict(context)
        rl = context.get("config", {}).get("resource_limits", {})
        try:
            self._timeout = float(rl.get("inference_timeout", 120))
        except (TypeError, ValueError):
            self._timeout = 120.0
        self._broken = False
        adapter.initialize(context)

    def infer(self, request: Dict[str, Any], request_id: str) -> Any:
        if self._broken or self._adapter is None:
            raise WorkerNotReady(f"worker not ready: {self._app_id}")
        context = dict(self._context)
        context["request_id"] = request_id
        future = self._executor.submit(self._adapter.infer, request, context)
        try:
            return future.result(timeout=self._timeout)
        except concurrent.futures.TimeoutError:
            raise InferenceTimeout(
                f"inference timeout after {self._timeout:.0f}s: {self._app_id}"
            )

    def stop(self, app_id: str) -> None:
        adapter = self._adapter
        if adapter is not None:
            try:
                adapter.shutdown()
            except Exception:
                pass

    def restart(self, app_id: str) -> None:
        if self._adapter is None or not self._context:
            raise RuntimeError(f"cannot restart {app_id}: never started")
        self._broken = False
        self._adapter.initialize(self._context)

    def status(self, app_id: str) -> str:
        if self._broken:
            return AppStatus.BACKOFF.value
        return AppStatus.READY.value if self._adapter is not None else AppStatus.IDLE.value

    def simulate_crash(self) -> None:
        """DEV-ONLY test hook: break this worker (isolation test P6)."""
        self._broken = True
        if self._readiness is not None:
            self._readiness.set(
                self._app_id, AppStatus.BACKOFF, detail="simulated crash (test hook)"
            )
        if self._audit is not None:
            self._audit.append(
                {
                    "action": "worker_crash",
                    "app_id": self._app_id,
                    "version": self._version,
                    "result": "simulated",
                }
            )

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False)
