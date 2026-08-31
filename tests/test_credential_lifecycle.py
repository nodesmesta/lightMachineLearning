"""Phase 2 Day-2 closure (P0-2/P0-3, 2026-08-31) — credential lifecycle tests
for SystemdTransientWorkerHost.

Reviewer P0-2 directive (verbatim): "Every credential file must disappear
after successful stop AND after every failed launch/start path. Jamaludin
should put credential cleanup into an exception-safe lifecycle (try/finally or
equivalent)."

Reviewer P0-3 requires deterministic tests for three paths:
  1. systemd-run launch failure -> credential removed;
  2. worker never becomes ready -> credential removed;
  3. normal stop -> credential removed.

These are NON-PRIVILEGED, deterministic unit tests: subprocess.run (the
systemd-run / systemctl calls) and the lifecycle hooks (_wait_ready,
_start_monitor, _stop_monitor) are mocked, so no root/systemd is required and
there is no timing dependence. The proofs assert both the on-disk file gone
AND the host's _credential_path cleared.
"""
from __future__ import annotations

import os
import tempfile
import types
import unittest
from unittest import mock

from sydeco_lightml_core.worker import SystemdTransientWorkerHost


def _mk_context(cred_dir: str) -> dict:
    return {
        "app_root": tempfile.mkdtemp(prefix="sydeco-cl-app-"),
        "port": 0,
        "config": {"resource_limits": {"inference_timeout": 5}},
        "credential_dir": cred_dir,
        "data_dir": tempfile.mkdtemp(prefix="sydeco-cl-data-"),
        "user": "",
    }


def _fake_run(returncode=0, stdout="", stderr=""):
    def _run(argv, **kwargs):
        return types.SimpleNamespace(
            returncode=returncode, stdout=stdout, stderr=stderr
        )
    return _run


class CredentialLifecycleTests(unittest.TestCase):

    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="sydeco-cl-")

    def _new_host(self, cred_dir: str) -> SystemdTransientWorkerHost:
        return SystemdTransientWorkerHost(
            ready_wait=1.0, poll_interval=0.05
        )

    # ---- case 1: systemd-run launch failure ------------------------------

    def test_launch_failure_removes_credential(self) -> None:
        cred_dir = os.path.join(self._tmp, "secrets")
        os.makedirs(cred_dir, exist_ok=True)
        host = self._new_host(cred_dir)
        ctx = _mk_context(cred_dir)

        # systemd-run fails (non-zero returncode) -> start() must raise AND
        # remove the credential it created before the launch attempt.
        with mock.patch(
            "subprocess.run", side_effect=_fake_run(returncode=1, stderr="boom")
        ):
            with self.assertRaises(RuntimeError):
                host.start("app-a", "1.0.0", None, ctx)

        # The ephemeral credential file must be GONE and the host must have
        # forgotten it.
        self.assertIsNone(host._credential_path)
        self.assertFalse(host._started)
        leftover = [
            os.path.join(cred_dir, f)
            for f in os.listdir(cred_dir)
            if f.endswith(".secret")
        ]
        self.assertEqual(leftover, [], "credential file left after launch failure")

    # ---- case 2: worker never becomes ready -----------------------------

    def test_never_ready_removes_credential(self) -> None:
        cred_dir = os.path.join(self._tmp, "secrets")
        os.makedirs(cred_dir, exist_ok=True)
        host = self._new_host(cred_dir)
        ctx = _mk_context(cred_dir)

        # systemd-run succeeds; then _wait_ready() returns False (worker never
        # becomes ready) -> start() raises RuntimeError after cleaning up.
        # subprocess.run is mocked to model systemctl stop/show as inactive so
        # the internal stop() on the failure path completes cleanly.
        def _run(argv, **kwargs):
            if argv and argv[0] == "systemd-run":
                return types.SimpleNamespace(returncode=0, stdout="", stderr="")
            # systemctl show active state -> "inactive" (no SIGKILL branch)
            if argv and argv[0] == "systemctl":
                return types.SimpleNamespace(
                    returncode=0,
                    stdout="inactive\n" if "-p" in argv else "",
                    stderr="",
                )
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        with (
            mock.patch("subprocess.run", side_effect=_run),
            mock.patch.object(host, "_wait_ready", return_value=False),
            mock.patch.object(host, "_start_monitor", return_value=None),
            mock.patch.object(host, "_stop_monitor", return_value=None),
        ):
            with self.assertRaises(RuntimeError):
                host.start("app-a", "1.0.0", None, ctx)

        self.assertIsNone(host._credential_path)
        leftover = [
            os.path.join(cred_dir, f)
            for f in os.listdir(cred_dir)
            if f.endswith(".secret")
        ]
        self.assertEqual(leftover, [], "credential file left after readiness failure")

    # ---- case 3: normal successful stop ---------------------------------

    def test_normal_stop_removes_credential(self) -> None:
        cred_dir = os.path.join(self._tmp, "secrets")
        os.makedirs(cred_dir, exist_ok=True)
        host = self._new_host(cred_dir)
        ctx = _mk_context(cred_dir)

        def _run(argv, **kwargs):
            if argv and argv[0] == "systemd-run":
                return types.SimpleNamespace(returncode=0, stdout="", stderr="")
            if argv and argv[0] == "systemctl":
                return types.SimpleNamespace(
                    returncode=0,
                    stdout="inactive\n" if "-p" in argv else "",
                    stderr="",
                )
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        with (
            mock.patch("subprocess.run", side_effect=_run),
            mock.patch.object(host, "_wait_ready", return_value=True),
            mock.patch.object(host, "_start_monitor", return_value=None),
            mock.patch.object(host, "_stop_monitor", return_value=None),
        ):
            host.start("app-a", "1.0.0", None, ctx)

        # After a successful start a credential file exists and is registered.
        self.assertIsNotNone(host._credential_path)
        self.assertTrue(os.path.exists(host._credential_path))
        self.assertTrue(host._started)

        with mock.patch("subprocess.run", side_effect=_run):
            host.stop("app-a")

        # After a normal stop the credential file must be GONE.
        self.assertIsNone(host._credential_path)
        self.assertFalse(host._started)
        leftover = [
            os.path.join(cred_dir, f)
            for f in os.listdir(cred_dir)
            if f.endswith(".secret")
        ]
        self.assertEqual(leftover, [], "credential file left after normal stop")


if __name__ == "__main__":
    unittest.main()