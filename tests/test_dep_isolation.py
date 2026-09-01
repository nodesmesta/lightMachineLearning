"""Phase 2 / Day 3 — J1/J2 per-application dependency isolation tests
(2026-08-31; reviewer P1/P2/P4/P5).

Proves the central Day-3 acceptance:
  - one Python venv per application/version (J1), built by Core
    (``CoreService.build_app_venv``) purely from the app's LOCAL wheelhouse;
  - the wheelhouse source is OFFLINE-only (J2): ``pip install --no-index
    --find-links=<wheelhouse>``, no PyPI / no network / no Core-env pickup;
  - two deterministic apps with INCOMPATIBLE versions of the same test
    dependency behave differently (App A -> v1, App B -> v2) and each runs
    under ITS OWN interpreter (P3), without modifying the Core environment;
  - failure & security paths fail CLOSED (missing wheel REJECT).

The ballsation package ``depballast`` is a stdlib-only fixture built AT RUNTIME
from source (offline, no network) so the test needs nothing but OS python +
ensurepip. Wording follows the standing rule: "needs no Internet beyond OS
dependencies."
"""
from __future__ import annotations

import json
import http.client
import os
import secrets
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from sydeco_lightml_core.core import CoreService
from tests._signing import sign_manifest

# ---- fixture: build a stdlib-only ballast wheel for a given version -----

_BALLAST_SRC = '''\
VERSION = "{version}"

def behavior():
    return "{version}-behavior"
'''

_SETUP_SRC = '''\
from setuptools import setup

setup(
    name="depballast",
    version="{version}",
    packages=["depballast"],
)
'''


def _build_ballast_wheel(tmp: str, version: str) -> str:
    """Build depballast-{version}-py3-none-any.whl offline into tmp/wheels."""
    src = os.path.join(tmp, f"ballast-src-{version}", "depballast")
    os.makedirs(src, exist_ok=True)
    with open(os.path.join(src, "__init__.py"), "w", encoding="utf-8") as fh:
        fh.write(_BALLAST_SRC.format(version=version))
    with open(
        os.path.join(src, "..", "setup.py"), "w", encoding="utf-8"
    ) as fh:
        fh.write(_SETUP_SRC.format(version=version))
    wheels = os.path.join(tmp, "wheels")
    os.makedirs(wheels, exist_ok=True)
    proj = os.path.dirname(src)  # the project dir containing setup.py
    subprocess.run(
        ["python3", "-m", "pip", "wheel", "--no-deps", "--no-build-isolation",
         "--wheel-dir", wheels, proj],
        capture_output=True, text=True, check=True, timeout=180,
    )
    match = [f for f in os.listdir(wheels) if f.startswith(f"depballast-{version}-")]
    if not match:
        raise RuntimeError(f"no wheel built for depballast {version}")
    return os.path.join(wheels, match[0])


def _manifest(app_id: str, version: str, dep_version: str) -> dict:
    return {
        "manifest_version": 1,
        "app_id": app_id,
        "name": app_id,
        "version": version,
        "capabilities": ["inference"],
        "models": [],
        "adapter": {"entry": "adapter/main.py", "files": []},
        "release": {"key_id": "sydeco-test-key-v1", "signature": "manifest.sig"},
        "input_schema": {"type": "object", "required": ["text"],
                         "properties": {"text": {"type": "string"}}},
        "output_schema": {"type": "object", "required": ["behavior"],
                          "properties": {"behavior": {"type": "string"}}},
        "permissions": {"network": "none"},
        "api": {"authentication": "token"},
        "resource_limits": {"max_memory": 1073741824, "max_cpu": 100,
                            "inference_timeout": 120, "concurrency": 1},
        "dependencies": [{"name": "depballast", "version": dep_version}],
    }


def _run_in(venv_python: str, code: str) -> str:
    proc = subprocess.run(
        [venv_python, "-c", code], capture_output=True, text=True, timeout=60
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"interpreter {venv_python} failed: {proc.stderr[-500:]}"
        )
    return proc.stdout.strip()


def _sha256_file(path: str) -> str:
    import hashlib

    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _request_worker(
    port: int,
    method: str,
    path: str,
    token: str,
    payload: dict | None = None,
    timeout: float = 5.0,
) -> tuple[int, dict]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    headers = {"Authorization": f"Bearer {token}"}
    body = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    conn.request(method, path, body=body, headers=headers)
    resp = conn.getresponse()
    raw = resp.read().decode("utf-8")
    conn.close()
    return resp.status, json.loads(raw) if raw else {}


def _wait_worker_ready(port: int, token: str, timeout_s: float = 10.0) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            status, _ = _request_worker(port, "GET", "/health/ready", token)
            if status == 200:
                return True
        except Exception:
            pass
        time.sleep(0.05)
    return False


def _spawn_worker(venv_python: str, app_root: str, port: int, cred_file: str):
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    env = dict(os.environ, PYTHONPATH=repo_root)
    return subprocess.Popen(
        [
            venv_python,
            "-m",
            "sydeco_lightml_core.worker_runtime",
            "--app-root",
            app_root,
            "--port",
            str(port),
            "--credential-file",
            cred_file,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
    )


def _make_end_to_end_bundle(
    tmp: str, app_id: str, dep_version: str, wheel_path: str
) -> tuple[str, dict]:
    import pickle

    bundle = os.path.join(tmp, app_id)
    os.makedirs(os.path.join(bundle, "adapter"), exist_ok=True)
    os.makedirs(os.path.join(bundle, "models"), exist_ok=True)
    os.makedirs(os.path.join(bundle, "wheelhouse"), exist_ok=True)
    shutil.copy(
        wheel_path,
        os.path.join(bundle, "wheelhouse", os.path.basename(wheel_path)),
    )

    adapter_src = (
        "import depballast\n\n"
        "class Adapter:\n"
        "    def initialize(self, context):\n"
        "        self._behavior = depballast.behavior()\n"
        "        self._version = depballast.VERSION\n"
        "    def infer(self, request, context):\n"
        "        return {'behavior': self._behavior, 'version': self._version}\n"
        "    def shutdown(self):\n"
        "        pass\n"
    )
    adapter_path = os.path.join(bundle, "adapter", "main.py")
    with open(adapter_path, "w", encoding="utf-8") as fh:
        fh.write(adapter_src)

    model_path = os.path.join(bundle, "models", "model.pkl")
    with open(model_path, "wb") as fh:
        pickle.dump({"dummy": True}, fh)

    manifest = {
        "manifest_version": 1,
        "app_id": app_id,
        "name": app_id,
        "version": "1.0.0",
        "capabilities": ["inference"],
        "models": [
            {
                "role": "model",
                "file": "models/model.pkl",
                "format": "pickle",
                "sha256": _sha256_file(model_path),
            }
        ],
        "adapter": {
            "entry": "adapter/main.py",
            "files": [{"file": "adapter/main.py", "sha256": _sha256_file(adapter_path)}],
        },
        "release": {"key_id": "sydeco-test-key-v1", "signature": "manifest.sig"},
        "input_schema": {
            "type": "object",
            "required": ["text"],
            "properties": {"text": {"type": "string"}},
        },
        "output_schema": {
            "type": "object",
            "required": ["behavior", "version"],
            "properties": {
                "behavior": {"type": "string"},
                "version": {"type": "string"},
            },
        },
        "permissions": {"network": "none"},
        "api": {"authentication": "token"},
        "resource_limits": {
            "max_memory": 1073741824,
            "max_cpu": 100,
            "inference_timeout": 120,
            "concurrency": 1,
        },
        "dependencies": [{"name": "depballast", "version": dep_version}],
    }
    with open(os.path.join(bundle, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)
    sign_manifest(bundle)
    return bundle, manifest


class DepIsolationTests(unittest.TestCase):

    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="sydeco-j12-")
        self._core = CoreService(
            data_dir=os.path.join(self._tmp, "core-data"), worker_mode="inprocess"
        )

    def tearDown(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_two_apps_offline_venv_isolated_behavior(self) -> None:
        """Reviewer P4 core proof: App A -> depballast v1, App B -> v2,
        each under its OWN never-interpreter; neither modifies the Core env."""
        w1 = _build_ballast_wheel(self._tmp, "1.0.0")
        w2 = _build_ballast_wheel(self._tmp, "2.0.0")

        root_a = os.path.join(self._tmp, "app-a")
        root_b = os.path.join(self._tmp, "app-b")
        for root, ver in ((root_a, "1.0.0"), (root_b, "2.0.0")):
            wh = os.path.join(root, "wheelhouse")
            os.makedirs(wh, exist_ok=True)
            shutil.copy(w1 if ver == "1.0.0" else w2, wh)

        ma = _manifest("app-a", "1.0.0", "1.0.0")
        mb = _manifest("app-b", "1.0.0", "2.0.0")

        # Core builds a venv per app from its local wheelhouse (offline).
        pa = self._core.build_app_venv("app-a", ma, root_a)
        pb = self._core.build_app_venv("app-b", mb, root_b)

        va = os.path.join(root_a, "venv", "bin", "python")
        vb = os.path.join(root_b, "venv", "bin", "python")

        # Distinct interpreters (separate venv per app) — P3.
        self.assertNotEqual(va, vb)
        self.assertTrue(os.path.isfile(va))
        self.assertTrue(os.path.isfile(vb))

        # Behavior differs — App A sees v1, App B sees v2.
        self.assertEqual(_run_in(va, "import depballast; print(depballast.behavior())"),
                         "1.0.0-behavior")
        self.assertEqual(_run_in(vb, "import depballast; print(depballast.behavior())"),
                         "2.0.0-behavior")
        # Version attribute matches chosen wheel.
        self.assertEqual(_run_in(va, "import depballast; print(depballast.VERSION)"),
                         "1.0.0")
        self.assertEqual(_run_in(vb, "import depballast; print(depballast.VERSION)"),
                         "2.0.0")

        # Registry-like filesystem_paths recorded venv + wheelhouse.
        self.assertEqual(pa["venv"], os.path.join(root_a, "venv"))
        self.assertEqual(pb["venv"], os.path.join(root_b, "venv"))

        # Core environment NOT modified: the system python cannot import
        # depballast (it only exists inside the per-app venvs).
        core_can_import = subprocess.run(
            ["python3", "-c", "import depballast"], capture_output=True, text=True
        ).returncode
        self.assertNotEqual(core_can_import, 0,
                            "Core/global python must NOT see app dependency")

    def test_breaking_one_app_does_not_affect_other(self) -> None:
        """Reviewer P4: breaking/removing A's environment does not affect B."""
        w1 = _build_ballast_wheel(self._tmp, "1.0.0")
        w2 = _build_ballast_wheel(self._tmp, "2.0.0")
        root_a = os.path.join(self._tmp, "app-a")
        root_b = os.path.join(self._tmp, "app-b")
        for root, ver in ((root_a, "1.0.0"), (root_b, "2.0.0")):
            wh = os.path.join(root, "wheelhouse")
            os.makedirs(wh, exist_ok=True)
            shutil.copy(w1 if ver == "1.0.0" else w2, wh)
        self._core.build_app_venv("app-a", _manifest("app-a", "1.0.0", "1.0.0"), root_a)
        ps = self._core.build_app_venv("app-b", _manifest("app-b", "1.0.0", "2.0.0"), root_b)
        vb = os.path.join(root_b, "venv", "bin", "python")
        b_before = _run_in(vb, "import depballast; print(depballast.behavior())")

        # Break app A's whole venv.
        shutil.rmtree(os.path.join(root_a, "venv"), ignore_errors=True)

        # App B still works, still v2.
        self.assertEqual(
            _run_in(vb, "import depballast; print(depballast.behavior())"), b_before)
        self.assertEqual(_run_in(vb, "import depballast; print(depballast.behavior())"),
                         "2.0.0-behavior")

    def test_dependency_free_app_still_gets_dedicated_venv(self) -> None:
        """J1: an app with no Python dependencies still gets its own venv."""
        root = os.path.join(self._tmp, "app-no-deps")
        os.makedirs(root, exist_ok=True)
        manifest = _manifest("app-no-deps", "1.0.0", "1.0.0")
        manifest["dependencies"] = []

        paths = self._core.build_app_venv("app-no-deps", manifest, root)
        app_python = os.path.join(root, "venv", "bin", "python")

        self.assertEqual(paths["venv"], os.path.join(root, "venv"))
        self.assertTrue(os.path.isfile(app_python))
        self.assertNotIn("wheelhouse", paths)

    def test_wheelhouse_symlink_escape_rejects(self) -> None:
        """Reviewer 01-09 P1: a wheelhouse symlink resolving outside the app
        root must be rejected fail-closed."""
        root = os.path.join(self._tmp, "app-symlink-wheelhouse")
        os.makedirs(root, exist_ok=True)
        outside = os.path.join(self._tmp, "outside-wheelhouse")
        os.makedirs(outside, exist_ok=True)
        wheel = _build_ballast_wheel(self._tmp, "1.0.0")
        shutil.copy(wheel, outside)
        os.symlink(outside, os.path.join(root, "wheelhouse"))

        with self.assertRaises(RuntimeError):
            self._core.build_app_venv(
                "app-symlink-wheelhouse",
                _manifest("app-symlink-wheelhouse", "1.0.0", "1.0.0"),
                root,
            )

    def test_wheel_file_symlink_escape_rejects(self) -> None:
        """Reviewer 01-09 P1: a dependency file symlink resolving outside the
        app root must be rejected fail-closed."""
        root = os.path.join(self._tmp, "app-symlink-wheel")
        wheelhouse = os.path.join(root, "wheelhouse")
        os.makedirs(wheelhouse, exist_ok=True)
        outside_dir = os.path.join(self._tmp, "outside-wheel-files")
        os.makedirs(outside_dir, exist_ok=True)
        outside_wheel = _build_ballast_wheel(self._tmp, "1.0.0")
        outside_copy = os.path.join(outside_dir, os.path.basename(outside_wheel))
        shutil.copy(outside_wheel, outside_copy)
        os.symlink(outside_copy, os.path.join(wheelhouse, os.path.basename(outside_copy)))

        with self.assertRaises(RuntimeError):
            self._core.build_app_venv(
                "app-symlink-wheel",
                _manifest("app-symlink-wheel", "1.0.0", "1.0.0"),
                root,
            )

    def test_missing_systemd_venv_interpreter_fails_closed(self) -> None:
        """Reviewer 01-09 P2: if an app owns a venv but its interpreter is
        missing, startup must fail closed instead of falling back to the Core
        Python."""
        service = CoreService(
            data_dir=os.path.join(self._tmp, "core-data-systemd"), worker_mode="systemd"
        )
        app_root = os.path.join(self._tmp, "app-systemd")
        venv_dir = os.path.join(app_root, "venv")
        os.makedirs(venv_dir, exist_ok=True)

        fake_manifest = _manifest("app-systemd", "1.0.0", "1.0.0")

        with mock.patch("sydeco_lightml_core.core.subprocess.run") as run_mock, \
             mock.patch.object(service.worker_manager, "start") as start_mock:
            run_mock.return_value = subprocess.CompletedProcess(args=["chown"], returncode=0)
            ok, message, _ = service._start_app_systemd(
                "app-systemd", fake_manifest, "1.0.0", app_root, venv_dir
            )

        self.assertFalse(ok)
        self.assertIn("venv", message.lower())
        start_mock.assert_not_called()

    def test_shell_injection_dependency_name_is_safe(self) -> None:
        """Reviewer 01-09 P5: the hostile dependency marker checked by the test
        must exactly match the path the hostile string attempts to create."""
        root = os.path.join(self._tmp, "app-x")
        wh = os.path.join(root, "wheelhouse")
        os.makedirs(wh, exist_ok=True)
        w1 = _build_ballast_wheel(self._tmp, "1.0.0")
        shutil.copy(w1, wh)
        m = _manifest("app-x", "1.0.0", "1.0.0")
        hostile = dict(m)
        marker = "/tmp/INJECTED"
        hostile["dependencies"] = [{"name": "$(touch /tmp/INJECTED)", "version": "1.0.0"}]
        try:
            if os.path.exists(marker):
                os.unlink(marker)
            with self.assertRaises(RuntimeError):
                self._core.build_app_venv("app-x", hostile, root)
        finally:
            if os.path.exists(marker):
                os.unlink(marker)
        self.assertFalse(os.path.exists(marker),
                         "hostile dependency name must not execute a command")

    def test_missing_wheel_rejects(self) -> None:
        """Reviewer P5: missing dependency wheel -> REJECT (fail-closed)."""
        root = os.path.join(self._tmp, "app-x")
        os.makedirs(os.path.join(root, "wheelhouse"), exist_ok=True)  # empty
        m = _manifest("app-x", "1.0.0", "9.9.9")  # dep NOT in wheelhouse
        with self.assertRaises(RuntimeError):
            self._core.build_app_venv("app-x", m, root)

    def test_corrupt_dependency_material_rejects(self) -> None:
        """Reviewer P5: corrupt dependency material -> REJECT (fail-closed,
        no half-valid environment left)."""
        root = os.path.join(self._tmp, "app-x")
        wh = os.path.join(root, "wheelhouse")
        os.makedirs(wh, exist_ok=True)
        # A wheel whose name parses as depballast 1.0.0 but is corrupt.
        with open(os.path.join(wh, "depballast-1.0.0-py3-none-any.whl"), "w",
                  encoding="utf-8") as fh:
            fh.write("this is not a valid zip/wheel")  # corrupt payload
        m = _manifest("app-x", "1.0.0", "1.0.0")
        with self.assertRaises(RuntimeError):
            self._core.build_app_venv("app-x", m, root)
        # No half-built environment survives.
        self.assertFalse(os.path.exists(os.path.join(root, "venv", "bin", "python")))

    def test_two_real_workers_infer_200_and_restart_preserves_isolation(self) -> None:
        """Reviewer 01-09 P5: run two real workers whose adapters import
        incompatible dependency versions and prove /infer -> 200 for both,
        before and after restart."""
        w1 = _build_ballast_wheel(self._tmp, "1.0.0")
        w2 = _build_ballast_wheel(self._tmp, "2.0.0")
        bundle_a, manifest_a = _make_end_to_end_bundle(self._tmp, "app-a-e2e", "1.0.0", w1)
        bundle_b, manifest_b = _make_end_to_end_bundle(self._tmp, "app-b-e2e", "2.0.0", w2)

        pa = self._core.build_app_venv("app-a-e2e", manifest_a, bundle_a)
        pb = self._core.build_app_venv("app-b-e2e", manifest_b, bundle_b)
        va = os.path.join(pa["venv"], "bin", "python")
        vb = os.path.join(pb["venv"], "bin", "python")

        port_a = _free_port()
        port_b = _free_port()
        cred_a = os.path.join(self._tmp, "a.secret")
        cred_b = os.path.join(self._tmp, "b.secret")
        token_a = secrets.token_hex(32)
        token_b = secrets.token_hex(32)
        with open(cred_a, "w", encoding="utf-8") as fh:
            fh.write(token_a + "\n")
        with open(cred_b, "w", encoding="utf-8") as fh:
            fh.write(token_b + "\n")

        procs = []
        try:
            proc_a = _spawn_worker(va, bundle_a, port_a, cred_a)
            proc_b = _spawn_worker(vb, bundle_b, port_b, cred_b)
            procs.extend([proc_a, proc_b])
            self.assertTrue(_wait_worker_ready(port_a, token_a), "worker A never became ready")
            self.assertTrue(_wait_worker_ready(port_b, token_b), "worker B never became ready")

            st_a, body_a = _request_worker(port_a, "POST", "/infer", token_a, {"text": "x"})
            st_b, body_b = _request_worker(port_b, "POST", "/infer", token_b, {"text": "x"})
            self.assertEqual(st_a, 200, body_a)
            self.assertEqual(st_b, 200, body_b)
            self.assertEqual(body_a["result"]["behavior"], "1.0.0-behavior")
            self.assertEqual(body_b["result"]["behavior"], "2.0.0-behavior")
            self.assertEqual(body_a["result"]["version"], "1.0.0")
            self.assertEqual(body_b["result"]["version"], "2.0.0")

            for proc in procs:
                proc.terminate()
                proc.wait(timeout=10)
            procs.clear()

            proc_a2 = _spawn_worker(va, bundle_a, port_a, cred_a)
            proc_b2 = _spawn_worker(vb, bundle_b, port_b, cred_b)
            procs.extend([proc_a2, proc_b2])
            self.assertTrue(_wait_worker_ready(port_a, token_a), "worker A restart never became ready")
            self.assertTrue(_wait_worker_ready(port_b, token_b), "worker B restart never became ready")
            st_a2, body_a2 = _request_worker(port_a, "POST", "/infer", token_a, {"text": "x"})
            st_b2, body_b2 = _request_worker(port_b, "POST", "/infer", token_b, {"text": "x"})
            self.assertEqual(st_a2, 200, body_a2)
            self.assertEqual(st_b2, 200, body_b2)
            self.assertEqual(body_a2["result"]["version"], "1.0.0")
            self.assertEqual(body_b2["result"]["version"], "2.0.0")
        finally:
            for proc in procs:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        proc.kill()

    def test_breaking_a_venv_makes_a_fail_closed_while_b_stays_ready(self) -> None:
        """Reviewer 01-09 P5: breaking App A's venv must make A refuse to
        start while App B remains READY and still returns 200."""
        w1 = _build_ballast_wheel(self._tmp, "1.0.0")
        w2 = _build_ballast_wheel(self._tmp, "2.0.0")
        bundle_a, manifest_a = _make_end_to_end_bundle(self._tmp, "app-a-failclosed", "1.0.0", w1)
        bundle_b, manifest_b = _make_end_to_end_bundle(self._tmp, "app-b-healthy", "2.0.0", w2)

        pa = self._core.build_app_venv("app-a-failclosed", manifest_a, bundle_a)
        pb = self._core.build_app_venv("app-b-healthy", manifest_b, bundle_b)
        va = os.path.join(pa["venv"], "bin", "python")
        vb = os.path.join(pb["venv"], "bin", "python")

        port_b = _free_port()
        cred_b = os.path.join(self._tmp, "b-healthy.secret")
        token_b = secrets.token_hex(32)
        with open(cred_b, "w", encoding="utf-8") as fh:
            fh.write(token_b + "\n")

        proc_b = _spawn_worker(vb, bundle_b, port_b, cred_b)
        try:
            self.assertTrue(_wait_worker_ready(port_b, token_b), "worker B never became ready")
            shutil.rmtree(pa["venv"], ignore_errors=True)
            self.assertFalse(os.path.exists(va), "App A interpreter should be gone after venv removal")

            cred_a = os.path.join(self._tmp, "a-failclosed.secret")
            token_a = secrets.token_hex(32)
            with open(cred_a, "w", encoding="utf-8") as fh:
                fh.write(token_a + "\n")
            with self.assertRaises(FileNotFoundError):
                _spawn_worker(va, bundle_a, _free_port(), cred_a)

            st_b, body_b = _request_worker(port_b, "POST", "/infer", token_b, {"text": "x"})
            self.assertEqual(st_b, 200, body_b)
            self.assertEqual(body_b["result"]["version"], "2.0.0")
        finally:
            if proc_b.poll() is None:
                proc_b.terminate()
                try:
                    proc_b.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc_b.kill()

    def test_failed_venv_construction_leaves_no_half_valid_environment(self) -> None:
        """Reviewer 01-09 P5: if venv creation itself fails, install must
        fail closed and leave no surviving app interpreter behind."""
        wheel = _build_ballast_wheel(self._tmp, "1.0.0")
        bundle, _manifest_obj = _make_end_to_end_bundle(
            self._tmp, "app-venv-build-fails", "1.0.0", wheel
        )
        manifest_path = os.path.join(bundle, "manifest.json")

        real_run = subprocess.run

        def fake_run(argv, *args, **kwargs):
            if argv[:3] == ["python3", "-m", "venv"]:
                raise subprocess.CalledProcessError(
                    1, argv, stderr="simulated venv create failure"
                )
            return real_run(argv, *args, **kwargs)

        with mock.patch("sydeco_lightml_core.core.subprocess.run", side_effect=fake_run):
            ok, message, entry = self._core.install_app(manifest_path, app_root=bundle)

        self.assertFalse(ok)
        self.assertIsNone(entry)
        self.assertIn("install rejected", message)
        self.assertFalse(
            os.path.exists(os.path.join(bundle, "venv", "bin", "python")),
            "failed venv construction must leave no half-valid interpreter",
        )

    def test_failed_install_leaves_no_falsely_active_registry_entry(self) -> None:
        """Reviewer 01-09 P5: a failed dependency-environment install must
        not create a registry entry, routing target, or active app record."""
        wheel = _build_ballast_wheel(self._tmp, "1.0.0")
        bundle, _manifest_obj = _make_end_to_end_bundle(
            self._tmp, "app-no-registry-on-failure", "1.0.0", wheel
        )
        manifest_path = os.path.join(bundle, "manifest.json")

        real_run = subprocess.run

        def fake_run(argv, *args, **kwargs):
            if argv[:3] == ["python3", "-m", "venv"]:
                raise subprocess.CalledProcessError(
                    1, argv, stderr="simulated venv create failure"
                )
            return real_run(argv, *args, **kwargs)

        with mock.patch("sydeco_lightml_core.core.subprocess.run", side_effect=fake_run):
            ok, message, entry = self._core.install_app(manifest_path, app_root=bundle)

        self.assertFalse(ok)
        self.assertIsNone(entry)
        self.assertIn("install rejected", message)
        self.assertIsNone(self._core.registry.get("app-no-registry-on-failure"))
        self.assertIsNone(self._core.router.resolve("app-no-registry-on-failure"))
        listed = [row["app_id"] for row in self._core.registry.list_apps()]
        self.assertNotIn("app-no-registry-on-failure", listed)

if __name__ == "__main__":
    unittest.main()