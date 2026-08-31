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
import json
import os
import secrets
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, Optional, Union

from .adapter import Adapter
from .health import AppStatus, ReadinessStore


class WorkerHost(abc.ABC):
    """Abstract worker host: start / stop / restart one capability worker."""

    @abc.abstractmethod
    def start(
        self,
        app_id: str,
        version: str,
        adapter: Optional[Union[Adapter, Callable[[], Adapter]]],
        context: Dict[str, Any],
    ) -> None:
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

    def start(
        self,
        app_id: str,
        version: str,
        adapter: Optional[Union[Adapter, Callable[[], Adapter]]],
        context: Dict[str, Any],
    ) -> None:
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
        self._adapter_factory: Optional[Callable[[], Adapter]] = None
        self._context: Dict[str, Any] = {}
        self._timeout = 120.0
        self._broken = False
        self._stopped = True
        self._recycling_generation: Optional[int] = None
        self._generation = 0
        # Day 2 (2026-08-28): in-memory per-generation credential, rotated
        # on every recycle (D5 #5 — test-only dev equivalent of the systemd
        # LoadCredential path; honest label, never written anywhere).
        self._secret: Optional[str] = None
        self._lifecycle_lock = threading.RLock()
        self._readiness = readiness
        self._audit = audit
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="sydeco-worker"
        )

    def start(
        self,
        app_id: str,
        version: str,
        adapter: Optional[Union[Adapter, Callable[[], Adapter]]],
        context: Dict[str, Any],
    ) -> None:
        assert adapter is not None, "InProcessWorkerHost requires an adapter"
        candidate: Optional[Adapter]
        with self._lifecycle_lock:
            self._app_id = app_id
            self._version = version
            if callable(adapter) and not isinstance(adapter, Adapter):
                self._adapter_factory = adapter
                candidate = None
            else:
                self._adapter_factory = None
                candidate = adapter
            self._context = dict(context)
            rl = context.get("config", {}).get("resource_limits", {})
            try:
                self._timeout = float(rl.get("inference_timeout", 120))
            except (TypeError, ValueError):
                self._timeout = 120.0
            self._generation += 1
            generation = self._generation
            self._recycling_generation = None
            self._broken = True
            self._stopped = False
            # fresh in-memory credential for this generation (D5 #5)
            self._secret = secrets.token_hex(32)
            factory = self._adapter_factory

        if candidate is None:
            assert factory is not None
            candidate = factory()
        candidate.initialize(context)

        with self._lifecycle_lock:
            stale = self._stopped or generation != self._generation
            if not stale:
                self._adapter = candidate
                self._broken = False
        if stale:
            try:
                candidate.shutdown()
            except Exception:
                pass
            raise WorkerNotReady(f"start generation superseded: {app_id}")

    def infer(self, request: Dict[str, Any], request_id: str) -> Any:
        with self._lifecycle_lock:
            if self._broken or self._stopped or self._adapter is None:
                raise WorkerNotReady(f"worker not ready: {self._app_id}")
            context = dict(self._context)
            context["request_id"] = request_id
            generation = self._generation
            adapter = self._adapter
            future = self._executor.submit(adapter.infer, request, context)
        try:
            result = future.result(timeout=self._timeout)
        except concurrent.futures.CancelledError as exc:
            raise WorkerNotReady(
                f"worker generation quarantined: {self._app_id}"
            ) from exc
        except concurrent.futures.TimeoutError:
            self._on_timeout(generation)
            raise InferenceTimeout(
                f"inference timeout after {self._timeout:.0f}s: {self._app_id}"
            )
        except Exception as exc:
            with self._lifecycle_lock:
                if self._stopped or generation != self._generation:
                    raise WorkerNotReady(
                        f"stale worker generation ignored: {self._app_id}"
                    ) from exc
            raise
        with self._lifecycle_lock:
            if self._stopped or generation != self._generation:
                raise WorkerNotReady(
                    f"stale worker generation ignored: {self._app_id}"
                )
        return result

    def _on_timeout(self, generation: int) -> None:
        """Quarantine a timed-out dev-host generation and recover in BACKOFF.

        ``InProcessWorkerHost`` is a test-only approximation. Python cannot
        safely terminate a running ThreadPoolExecutor inference, and
        ``shutdown(wait=False)`` only abandons that executor. Hard containment
        exists only in the production systemd host, which kills the worker
        process. Recovery therefore requires an adapter factory: all future
        work moves to a fresh adapter + executor generation, while completion
        of the abandoned generation is ignored.
        """
        with self._lifecycle_lock:
            if (
                self._stopped
                or generation != self._generation
                or self._recycling_generation is not None
            ):
                return
            self._generation += 1
            replacement_generation = self._generation
            self._recycling_generation = replacement_generation
            if self._audit is not None:
                self._audit.append(
                    {
                        "action": "INFERENCE_TIMEOUT",
                        "app_id": self._app_id,
                        "version": self._version,
                        "result": "detected",
                        "detail": f"no response within {self._timeout:.0f}s",
                    }
                )
            self._broken = True
            if self._readiness is not None:
                self._readiness.set(
                    self._app_id, AppStatus.BACKOFF, detail="timeout recycle"
                )
            abandoned_executor = self._executor
            self._executor = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="sydeco-worker"
            )
            abandoned_executor.shutdown(wait=False, cancel_futures=True)
        threading.Thread(
            target=self._recycle_worker,
            args=(replacement_generation,),
            daemon=True,
            name=f"sydeco-recycle-{self._app_id}",
        ).start()

    def _recycle_worker(self, generation: int) -> None:
        replacement: Optional[Adapter] = None
        try:
            with self._lifecycle_lock:
                factory = self._adapter_factory
                context = dict(self._context)
            if factory is None:
                raise RuntimeError(
                    "cannot recover timed-out in-process adapter without a factory"
                )
            replacement = factory()
            replacement.initialize(context)
            with self._lifecycle_lock:
                if (
                    self._stopped
                    or generation != self._generation
                    or self._recycling_generation != generation
                ):
                    return
                self._adapter = replacement
                # the replacement generation gets a FRESH in-memory
                # credential; the old one is dead (D5 #5)
                self._secret = secrets.token_hex(32)
                if self._audit is not None:
                    self._audit.append(
                        {
                            "action": "WORKER_RESTART",
                            "app_id": self._app_id,
                            "version": self._version,
                            "result": "detected",
                            "detail": "timeout recycle",
                        }
                    )
                # READY is committed under the lifecycle lock and after the
                # audit, so stop/restart cannot interleave with this commit.
                if self._readiness is not None:
                    self._readiness.set(self._app_id, AppStatus.READY)
                self._recycling_generation = None
                self._broken = False
                replacement = None
        except Exception:
            with self._lifecycle_lock:
                if (
                    not self._stopped
                    and generation == self._generation
                    and self._recycling_generation == generation
                    and self._audit is not None
                ):
                    # Readiness stays BACKOFF; the app stays gated (503).
                    self._audit.append(
                        {
                            "action": "WORKER_CRASH",
                            "app_id": self._app_id,
                            "version": self._version,
                            "result": "detected",
                            "detail": "timeout recycle failed",
                        }
                    )
        finally:
            if replacement is not None:
                try:
                    replacement.shutdown()
                except Exception:
                    pass
            with self._lifecycle_lock:
                if self._recycling_generation == generation:
                    self._recycling_generation = None

    def stop(self, app_id: str) -> None:
        with self._lifecycle_lock:
            self._generation += 1
            self._recycling_generation = None
            self._broken = True
            self._stopped = True
            adapter = self._adapter
            abandoned_executor = self._executor
            self._executor = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="sydeco-worker"
            )
        abandoned_executor.shutdown(wait=False, cancel_futures=True)
        if adapter is not None:
            try:
                adapter.shutdown()
            except Exception:
                pass

    def restart(self, app_id: str) -> None:
        with self._lifecycle_lock:
            if self._adapter is None or not self._context:
                raise RuntimeError(f"cannot restart {app_id}: never started")
            self._generation += 1
            generation = self._generation
            self._recycling_generation = None
            self._broken = True
            self._stopped = False
            # fresh in-memory credential for the new generation (D5 #5)
            self._secret = secrets.token_hex(32)
            factory = self._adapter_factory
            current = self._adapter
            context = dict(self._context)

        replacement = factory() if factory is not None else current
        assert replacement is not None
        replacement.initialize(context)

        with self._lifecycle_lock:
            stale = self._stopped or generation != self._generation
            if not stale:
                self._adapter = replacement
                self._broken = False
        if stale:
            if factory is not None:
                try:
                    replacement.shutdown()
                except Exception:
                    pass
            raise WorkerNotReady(f"restart generation superseded: {app_id}")

    def status(self, app_id: str) -> str:
        with self._lifecycle_lock:
            if self._stopped:
                return AppStatus.IDLE.value
            if self._broken:
                return AppStatus.BACKOFF.value
            return AppStatus.READY.value if self._adapter is not None else AppStatus.IDLE.value

    def simulate_crash(self) -> None:
        """DEV-ONLY test hook: break this worker (isolation test P6)."""
        with self._lifecycle_lock:
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
        self.stop(self._app_id)
        with self._lifecycle_lock:
            self._executor.shutdown(wait=False, cancel_futures=True)


def allocate_port(host: str = "127.0.0.1") -> int:
    """D2 req 1 (user decision 2026-08-24): CORE assigns the worker's
    loopback endpoint before launch. Best-effort free-port probe on
    127.0.0.1 (small bind race is acceptable in the dev harness)."""
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((host, 0))
        return int(s.getsockname()[1])
    finally:
        s.close()


class SystemdTransientWorkerHost(WorkerHost):
    """A3/3.3 production-path worker host (D1c, user decision 2026-08-24).

    Each capability worker runs as a systemd TRANSIENT unit launched via
    ``systemd-run``, so systemd owns the process AND its cgroup. No
    permanent unit files / daemon-reload / enable today; the property
    set is 1:1 reusable by the production unit file on a later day.

    Core-side responsibilities:
    - assign the worker loopback port BEFORE launch (D2 req 1)
    - launch the unit with the manifest-derived properties: User=
      (dedicated capability OS user, D3a), MemoryMax + MemoryOOMGroup,
      CPUQuota, TasksMax, ProtectSystem=strict, PrivateTmp,
      NoNewPrivileges, RestrictAddressFamilies, Restart=on-failure,
      StartLimitBurst/StartLimitIntervalSec (crash-loop backoff),
      TimeoutStopSec (bounded shutdown)
    - poll GET /health/ready on 127.0.0.1:<port> (G1, G3 bounded wait)
    - forward inference via HTTP with the manifest inference_timeout
      (M4); expiry -> InferenceTimeout -> 504 at the serving layer
    - monitor the unit state and emit the GRANULAR C2 audit events:
      MEMORY_LIMIT_EXCEEDED (Result=oom-kill), CPU_THROTTLED (cpu.stat
      nr_throttled), PID_LIMIT_REACHED (pids.current >= pids.max),
      WORKER_CRASH (unit failed), WORKER_RESTART (NRestarts increased),
      CRASH_LOOP_DETECTED (Result=start-limit-hit)
    - bounded stop: systemctl stop (SIGTERM + TimeoutStopSec grace),
      then SIGKILL fallback

    Requires root (systemd-run against the system manager); used by the
    privileged acceptance harness. InProcessWorkerHost stays the default
    for the non-privileged unit tests (no regression, acceptance 12).
    """

    def __init__(
        self,
        readiness: Optional[ReadinessStore] = None,
        audit=None,
        *,
        grace_seconds: float = 30.0,
        poll_interval: float = 0.5,
        ready_wait: float = 30.0,
        unit_prefix: str = "sydeco-cap",
    ) -> None:
        self._readiness = readiness
        self._audit = audit
        self._grace_seconds = float(grace_seconds)
        self._poll_interval = float(poll_interval)
        self._ready_wait = float(ready_wait)
        self._unit_prefix = unit_prefix
        self._app_id = ""
        self._version = ""
        self._unit = ""
        self._port = 0
        self._timeout = 120.0
        self._started = False
        self._ready = False
        self._last_context: Dict[str, Any] = {}
        self._monitor = None
        self._stop_event = None
        self._last_nrestarts = 0
        self._last_throttled = 0
        self._pids_flagged = False
        self._recycling = False
        # Day 2 (2026-08-28): the check/set of _recycling must be atomic —
        # the Core HTTP server is multithreaded and two simultaneous timeouts
        # could otherwise spawn two recycle threads (reviewer P0). Small
        # local lock only; no WorkerManager redesign (D4).
        self._recycle_lock = threading.Lock()
        # per-generation credential (P1/P2): in-memory on the Core side,
        # delivered to the worker ONLY via systemd LoadCredential= (P3).
        self._secret: Optional[str] = None
        self._credential_path: Optional[str] = None
        self._launch_count = 0

    # ---- lifecycle -----------------------------------------------------

    def _remove_credential(self) -> None:
        """Idempotent P3 cleanup: always delete the ephemeral credential
        file if one was created, then forget it. Called from stop() on
        EVERY path (including when _started is False) and from start()
        on every FAILED launch path (reviewer P0-2: every credential file
        must disappear after successful stop AND after every failed
        launch/start path). Best-effort — OSError is swallowed so a
        missing file never masks the lifecycle result."""
        if self._credential_path is not None:
            try:
                os.unlink(self._credential_path)
            except OSError:
                pass
            self._credential_path = None

    def start(
        self,
        app_id: str,
        version: str,
        adapter: Optional[Union[Adapter, Callable[[], Adapter]]],
        context: Dict[str, Any],
    ) -> None:
        """Launch the worker as a systemd transient unit and wait ready.

        ``adapter`` is None in systemd mode (the adapter lives INSIDE the
        worker process — Core never imports it); context carries the
        paths: app_root, config (manifest), data_dir, port, user,
        python (optional).
        """
        import subprocess

        self._app_id = app_id
        self._version = version
        self._port = int(context["port"])
        manifest = context.get("config", {})
        rl = manifest.get("resource_limits", {})
        try:
            self._timeout = float(rl.get("inference_timeout", 120))
        except (TypeError, ValueError):
            self._timeout = 120.0
        user = context.get("user", "")
        app_root = context["app_root"]
        data_dir = context.get("data_dir")
        cwd = context.get("cwd", "")
        pythonpath = context.get("pythonpath", "")
        # Unique unit name per launch: launch #1 keeps the canonical
        # sydeco-cap-<app_id> (Day-1 evidence format); every RELAUNCH (e.g.
        # the Day-1B timeout recycle) gets a fresh name -r1, -r2, ... so
        # systemd-run never collides with an existing transient unit and
        # each run has its own clean journal/cgroup (24-08 pitfall).
        if self._launch_count == 0:
            unit = f"{self._unit_prefix}-{app_id}"
        else:
            unit = f"{self._unit_prefix}-{app_id}-r{self._launch_count}"
        self._launch_count += 1
        self._unit = unit
        self._last_context = dict(context)

        # Day 2 (P1/P2): fresh cryptographically random secret per worker
        # generation (secrets.token_hex(32) == token_bytes(32), hex).
        self._secret = secrets.token_hex(32)
        # P3 delivery: ephemeral root-only (0600) file in the Core secrets
        # area, unique per launch. The worker NEVER sees this path — systemd
        # (root) reads it and exposes it as $CREDENTIALS_DIRECTORY/
        # worker-secret inside the unit. Removed in stop().
        self._credential_path = None
        credential_dir = context.get("credential_dir")
        if credential_dir:
            os.makedirs(credential_dir, exist_ok=True)
            cred_path = os.path.join(
                credential_dir, f"{app_id}-{self._launch_count}.secret"
            )
            fd = os.open(cred_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(self._secret + "\n")
            os.chmod(cred_path, 0o600)
            self._credential_path = cred_path

        python = context.get("python", sys.executable)
        cmd = [
            python, "-m", "sydeco_lightml_core.worker_runtime",
            "--app-root", app_root, "--port", str(self._port),
        ]
        if data_dir:
            cmd += ["--data-dir", data_dir]

        max_memory = rl.get("max_memory")
        props = []
        if user:
            props.append(f"User={user}")
        if max_memory:
            props.append(f"MemoryMax={int(max_memory)}")
            # MemorySwapMax=0 makes MemoryMax a TRUE hard limit: without
            # it the kernel throttles the worker at the limit instead of
            # OOM-killing when swap is available (verified 2026-08-24).
            props.append("MemorySwapMax=0")
        # systemd 249 (this host): OOMPolicy=kill makes systemd SIGKILL
        # the unit's processes when the cgroup hits MemoryMax (the
        # whole worker workload dies, no orphan children — the
        # MemoryOOMGroup equivalent on older systemd). OOMScoreAdjust
        # biases the OOM killer towards the worker.
        props.append("OOMPolicy=kill")
        props.append("OOMScoreAdjust=1000")
        props.append(f"CPUQuota={rl.get('max_cpu', 100)}%")
        props.append(f"TasksMax={rl.get('max_tasks', 64)}")
        props += [
            "ProtectSystem=strict",
            "PrivateTmp=yes",
            "NoNewPrivileges=yes",
            "RestrictAddressFamilies=AF_INET",
            "Restart=on-failure",
            "StartLimitBurst=3",
            "StartLimitIntervalSec=10",
            f"TimeoutStopSec={int(self._grace_seconds)}",  # seconds (249 shows '30s')
        ]
        if data_dir:
            # R7a / L4: the app's OWN data dir is the ONLY writable path
            # under ProtectSystem=strict (production pattern 3.3).
            props.append(f"ReadWritePaths={data_dir}")
        if cwd:
            props.append(f"WorkingDirectory={cwd}")
        if pythonpath:
            props.append(f"Environment=PYTHONPATH={pythonpath}")
        if self._credential_path:
            # systemd 249 has no --load-credential CLI option; the unit
            # property is set via the generic transient-unit mechanism
            # (reviewer P2: LoadCredential=, NOT command line / env).
            props.append(f"LoadCredential=worker-secret:{self._credential_path}")

        argv = (
            ["systemd-run", f"--unit={unit}"]
            + [f"--property={p}" for p in props]
            + ["--", *cmd]
        )
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=30)
            if proc.returncode != 0:
                raise RuntimeError(
                    f"systemd-run failed for {app_id}: "
                    f"{(proc.stderr or proc.stdout).strip()}"
                )
        except BaseException:
            # P0-2 (reviewer): a FAILED launch MUST still clean up the
            # ephemeral credential file -- _started is still False here so
            # stop() would otherwise return without unlinking it.
            self._remove_credential()
            raise
        self._started = True
        self._ready = False
        self._last_nrestarts = 0
        self._last_throttled = 0
        self._pids_flagged = False
        self._oom_checked = False
        self._start_monitor()
        try:
            if not self._wait_ready():
                self._audit_event("WORKER_CRASH", detail="worker not ready within wait")
                raise RuntimeError(
                    f"worker {app_id} not ready within {self._ready_wait:.0f}s"
                )
        except BaseException:
            # P0-2 (reviewer): a worker that launches but never becomes ready
            # must also have its credential removed.
            self._remove_credential()
            # The transient unit was created; stop it so no orphan worker
            # keeps running. _started is True, so stop() does the full
            # systemctl stop + credential cleanup.
            try:
                self.stop(app_id)
            except BaseException:
                pass
            raise
        self._ready = True

    def stop(self, app_id: str) -> None:
        """Bounded stop: SIGTERM + TimeoutStopSec grace, SIGKILL fallback."""
        import subprocess

        self._stop_monitor()
        # P0-2 (reviewer): the credential file is ALWAYS cleaned up, even
        # when the host was never _started (e.g. a failed launch). Previously
        # this cleanup was after the `if not self._started: return` guard, so
        # a failed launch could leave a root-only 0600 credential file behind.
        self._remove_credential()
        if not self._started:
            return
        unit = self._unit + ".service"
        subprocess.run(
            ["systemctl", "stop", unit],
            capture_output=True, text=True, timeout=60,
        )
        out = subprocess.run(
            ["systemctl", "show", unit, "-p", "ActiveState", "--value"],
            capture_output=True, text=True, timeout=30,
        )
        if out.stdout.strip() in ("active", "activating", "reloading"):
            subprocess.run(
                ["systemctl", "kill", "-s", "SIGKILL", unit],
                capture_output=True, text=True, timeout=30,
            )
        self._started = False
        self._ready = False
        if self._readiness is not None:
            self._readiness.set(app_id, AppStatus.IDLE)

    def restart(self, app_id: str) -> None:
        """Stop then start again with the stored context."""
        if not self._started or not self._last_context:
            raise RuntimeError(f"cannot restart {app_id}: never started")
        ctx = dict(self._last_context)
        self.stop(app_id)
        self.start(app_id, self._version, None, ctx)

    def status(self, app_id: str) -> str:
        if not self._started:
            return AppStatus.IDLE.value
        return AppStatus.READY.value if self._ready else AppStatus.LOADING.value

    def simulate_crash(self) -> None:
        """DEV-ONLY test hook: SIGKILL the worker (isolation / restart
        acceptance). systemd Restart=on-failure brings it back; the
        monitor audits WORKER_CRASH + WORKER_RESTART."""
        import subprocess

        if self._started:
            subprocess.run(
                ["systemctl", "kill", "-s", "SIGKILL", self._unit + ".service"],
                capture_output=True, text=True, timeout=30,
            )

    def shutdown(self) -> None:
        self._stop_monitor()
        self._started = False

    # ---- inference forwarding (M4 at the Core edge) --------------------

    def infer(self, request: Dict[str, Any], request_id: str) -> Any:
        """Forward to the worker on 127.0.0.1:<port> with the manifest
        inference_timeout; expiry -> InferenceTimeout (504 upstream)."""
        import http.client
        import socket

        if not self._started or not self._ready:
            raise WorkerNotReady(f"worker not ready: {self._app_id}")
        body = json.dumps(request).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self._secret is not None:
            # Day 2 (P3/P4): every Core->worker call carries the current
            # generation's Bearer credential (never logged, never in argv).
            headers["Authorization"] = "Bearer " + self._secret
        conn = None
        try:
            conn = http.client.HTTPConnection(
                "127.0.0.1", self._port, timeout=self._timeout
            )
            conn.request(
                "POST", "/infer", body=body, headers=headers
            )
            resp = conn.getresponse()
            data = resp.read()
            conn.close()
        except socket.timeout:
            # close the half-open connection (the client-side timeout does
            # not reach conn.close() below) — no resource leak per timeout
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
            self._audit_event(
                "INFERENCE_TIMEOUT",
                detail=f"no response within {self._timeout:.0f}s",
            )
            # Day 1B (2026-08-26): 504 alone is not containment — the stuck
            # adapter must not keep occupying the single-flight worker.
            # Recycle the capability worker via the existing A3 restart
            # path; readiness is false until the replacement is ready.
            self._recycle_after_timeout()
            raise InferenceTimeout(
                f"inference timeout after {self._timeout:.0f}s: {self._app_id}"
            )
        except (ConnectionRefusedError, ConnectionResetError, OSError):
            raise WorkerNotReady(f"worker unreachable: {self._app_id}")
        try:
            obj = json.loads(data.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise WorkerNotReady(f"worker returned invalid response: {self._app_id}")
        if resp.status != 200:
            raise RuntimeError(
                f"worker error {resp.status}: {obj.get('error', obj)}"
            )
        return obj["result"]

    # ---- timeout recycle (Day 1B, 2026-08-26) --------------------------

    def _recycle_after_timeout(self) -> None:
        """504 -> readiness false -> recycle the worker in the background.

        The 504 is returned promptly (the recycle runs on a daemon thread);
        the readiness store shows BACKOFF until the replacement worker is
        READY again — the reviewer's preferred behaviour, verbatim:
        "Inference timeout -> return 504 -> terminate/recycle that
        capability worker -> restart cleanly -> readiness false until the
        replacement worker is ready."
        """
        import threading

        # P0 (reviewer, 2026-08-28): the Core HTTP server is multithreaded —
        # two simultaneous timeouts on the SAME capability must produce
        # EXACTLY ONE recycle. The check/set of _recycling is atomic under
        # the local lock; a concurrent second caller sees _recycling=True
        # and returns without spawning a second recycle thread.
        with self._recycle_lock:
            if self._recycling:
                return
            self._recycling = True
        self._ready = False
        if self._readiness is not None:
            self._readiness.set(
                self._app_id, AppStatus.BACKOFF, detail="timeout recycle"
            )
        threading.Thread(
            target=self._recycle_worker, daemon=True,
            name=f"sydeco-recycle-{self._app_id}",
        ).start()

    def _recycle_worker(self) -> None:
        """Background: stop + relaunch the transient unit (existing A3
        restart path), re-poll /health/ready, then mark READY."""
        try:
            self.restart(self._app_id)
            self._audit_event("WORKER_RESTART", detail="timeout recycle")
            # READY last (see InProcessWorkerHost._recycle_worker).
            if self._readiness is not None:
                self._readiness.set(self._app_id, AppStatus.READY)
        except Exception:
            # Readiness stays BACKOFF; the monitor audits the failure.
            self._audit_event("WORKER_CRASH", detail="timeout recycle failed")
        finally:
            with self._recycle_lock:
                self._recycling = False

    # ---- readiness polling (G1) ----------------------------------------

    def _wait_ready(self) -> bool:
        import http.client
        import time

        deadline = time.time() + self._ready_wait
        headers = {}
        if self._secret is not None:
            headers["Authorization"] = "Bearer " + self._secret
        while time.time() < deadline:
            try:
                conn = http.client.HTTPConnection("127.0.0.1", self._port, timeout=2)
                conn.request("GET", "/health/ready", headers=headers)
                resp = conn.getresponse()
                resp.read()
                conn.close()
                if resp.status == 200:
                    return True
            except Exception:
                pass
            time.sleep(self._poll_interval)
        return False

    # ---- supervision / audit ------------------------------------------

    def _start_monitor(self) -> None:
        import threading

        self._stop_event = threading.Event()
        self._monitor = threading.Thread(
            target=self._monitor_loop, daemon=True,
            name=f"sydeco-mon-{self._app_id}",
        )
        self._monitor.start()

    def _stop_monitor(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()
        if self._monitor is not None:
            self._monitor.join(timeout=2.0)
        self._monitor = None
        self._stop_event = None

    def _monitor_loop(self) -> None:
        while self._stop_event is not None and not self._stop_event.is_set():
            try:
                self._check_unit_state()
            except Exception:
                pass
            self._stop_event.wait(self._poll_interval * 2)

    def _check_unit_state(self) -> None:
        import os
        import subprocess

        if not self._started:
            return
        # full unit name (".service" suffix) — `systemctl show` without
        # it can fail to resolve the transient unit -> empty output.
        unit = self._unit + ".service"
        show = subprocess.run(
            ["systemctl", "show", unit,
             "-p", "ActiveState", "-p", "Result", "-p", "NRestarts"],
            capture_output=True, text=True, timeout=15,
        )
        # parse per-property (no --value): robust to EMPTY values — with
        # --value a trailing empty property disappears from splitlines()
        # and the old len()<4 guard silently dropped the whole check
        # (monitor never audited WORKER_RESTART on an ACTIVE unit).
        props: Dict[str, str] = {}
        for line in show.stdout.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                props[k] = v
        active = props.get("ActiveState", "")
        result = props.get("Result", "")
        try:
            n = int(props.get("NRestarts", "0"))
        except ValueError:
            n = self._last_nrestarts
        if n > self._last_nrestarts:
            self._last_nrestarts = n
            self._audit_event("WORKER_RESTART", detail=f"systemd NRestarts={n}")

        if active == "failed":
            oom = result == "oom-kill"
            if not oom and not self._oom_checked:
                # Result=oom-kill can be overwritten by a fast
                # Restart=on-failure cycle (next Result=signal) — check
                # the journal ONCE per start for the OOM marker.
                oom = self._journal_says_oom()
                self._oom_checked = True
            if oom:
                self._audit_event(
                    "MEMORY_LIMIT_EXCEEDED",
                    detail="cgroup OOM kill (MemoryMax + OOMPolicy=kill)",
                )
            elif result == "start-limit-hit" or n >= 3:
                # crash-loop: systemd stopped restarting (StartLimitBurst
                # reached) OR the unit has already restarted >=3 times
                # and is now failed — protection engaged.
                self._audit_event(
                    "CRASH_LOOP_DETECTED",
                    detail=f"unit failed after {n} restarts, Result={result}",
                )
            else:
                self._audit_event(
                    "WORKER_CRASH", detail=f"unit failed, Result={result}"
                )
            if self._ready:
                self._ready = False
                if self._readiness is not None:
                    self._readiness.set(
                        self._app_id, AppStatus.BACKOFF, detail="worker failed"
                    )

        # CPU_THROTTLED: cgroup cpu.stat throttling counters (CPUQuota
        # semantics = THROTTLE, never termination — locked 2026-08-24).
        cgroup = f"/sys/fs/cgroup/system.slice/{self._unit}.service"
        cpu_stat = os.path.join(cgroup, "cpu.stat")
        if os.path.isfile(cpu_stat):
            try:
                with open(cpu_stat, "r", encoding="utf-8") as fh:
                    data = {
                        parts[0]: parts[1]
                        for line in fh
                        if (parts := line.split()) and len(parts) == 2
                    }
                nr = int(data.get("nr_throttled", 0))
                if nr > self._last_throttled:
                    self._last_throttled = nr
                    self._audit_event(
                        "CPU_THROTTLED", detail=f"nr_throttled={nr}"
                    )
            except (OSError, ValueError):
                pass

        # PID_LIMIT_REACHED: pids.max hit (blocking, not killing).
        pids_max = os.path.join(cgroup, "pids.max")
        pids_cur = os.path.join(cgroup, "pids.current")
        if os.path.isfile(pids_max) and os.path.isfile(pids_cur):
            try:
                with open(pids_max, "r", encoding="utf-8") as fh:
                    pmax = fh.read().strip()
                with open(pids_cur, "r", encoding="utf-8") as fh:
                    pcur = fh.read().strip()
                if pmax != "max" and pmax.isdigit() and not self._pids_flagged:
                    if int(pcur) >= int(pmax):
                        self._pids_flagged = True
                        self._audit_event(
                            "PID_LIMIT_REACHED",
                            detail=f"pids.current={pcur} >= pids.max={pmax}",
                        )
            except OSError:
                pass

    def _journal_says_oom(self) -> bool:
        """True when the unit journal records an OOM-kill event.

        systemd journal line: "<unit>: A process of this unit has been
        killed by the OOM killer." Used when Result=oom-kill was
        overwritten by a fast Restart=on-failure cycle.
        """
        import subprocess

        try:
            out = subprocess.run(
                ["journalctl", "-u", self._unit + ".service",
                 "--no-pager", "-o", "cat", "-n", "200"],
                capture_output=True, text=True, timeout=10,
            )
            return "killed by the OOM killer" in out.stdout
        except Exception:
            return False

    def _audit_event(self, event: str, detail: str = "") -> None:
        if self._audit is None:
            return
        try:
            self._audit.append(
                {
                    "action": event,
                    "app_id": self._app_id,
                    "version": self._version,
                    "result": "detected",
                    "detail": detail,
                }
            )
        except Exception:
            pass
