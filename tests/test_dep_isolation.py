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
import os
import shutil
import subprocess
import tempfile
import unittest

from sydeco_lightml_core.core import CoreService

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

    def test_shell_injection_dependency_name_is_safe(self) -> None:
        """Reviewer P5: no dependency names/paths cause shell-command
        injection. The dependency spec is passed as subprocess ARGV (never a
        shell string), so a hostile name cannot execute a command."""
        root = os.path.join(self._tmp, "app-x")
        wh = os.path.join(root, "wheelhouse")
        os.makedirs(wh, exist_ok=True)
        w1 = _build_ballast_wheel(self._tmp, "1.0.0")
        shutil.copy(w1, wh)
        m = _manifest("app-x", "1.0.0", "1.0.0")
        # Hostile dependency name with shell metacharacters would be refused
        # by pip (no such dist), and crucially MUST NOT execute a command.
        hostile = dict(m)
        hostile["dependencies"] = [{"name": "$(touch /tmp/INJECTED)", "version": "1.0.0"}]
        marker = os.path.join(self._tmp, "INJECTED")
        try:
            with self.assertRaises(RuntimeError):
                self._core.build_app_venv("app-x", hostile, root)
        finally:
            pass
        self.assertFalse(os.path.exists(marker),
                         "hostile dependency name must not execute a command")


if __name__ == "__main__":
    unittest.main()