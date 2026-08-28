"""Day 1 security tests (GeneralTask Day 1 list).

The installer/parser must REJECT each of these 10 cases, with the
rejection reason mapped to the proposal clause:

 1. missing manifest
 2. invalid JSON
 3. invalid app_id
 4. unknown capability
 5. missing adapter
 6. missing model
 7. incorrect SHA-256
 8. undeclared model artifact
 9. path traversal such as ../../file
10. unsupported model format

Each test runs the FULL install flow (CoreService.install_app) against
a temporary app dir and asserts rejection + (where applicable) audit
event. Stdlib unittest only.
"""
from __future__ import annotations

import copy
import json
import os
import shutil
import tempfile
import unittest

from sydeco_lightml_core.core import CoreService

from tests._signing import sign_manifest

VALID_MANIFEST = {
    "manifest_version": 1,
    "app_id": "sec-test-app",
    "name": "Security Test App",
    "version": "1.0.0",
    "capabilities": ["inference"],
    "models": [
        {
            "role": "model",
            "file": "models/model.pkl",
            "format": "pickle",
            "sha256": "0" * 64,
        }
    ],
    "adapter": {
        "entry": "adapter/main.py",
        "files": [{"file": "adapter/main.py", "sha256": "1" * 64}],
    },
    "release": {"key_id": "sydeco-test-key-v1", "signature": "manifest.sig"},
    "input_schema": {
        "type": "object",
        "required": ["text"],
        "properties": {"text": {"type": "string"}},
    },
    "output_schema": {
        "type": "object",
        "required": ["status"],
        "properties": {"status": {"type": "string"}},
    },
    "permissions": {"network": "none"},
    "api": {"authentication": "token"},
    "resource_limits": {
        "max_memory": 1073741824,
        "max_cpu": 100,
        "inference_timeout": 120,
        "concurrency": 1,
    },
    "dependencies": [],
}


class SecurityTests(unittest.TestCase):
    """Ten Day-1 rejection cases."""

    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="sydeco-sec-")
        self.data_dir = os.path.join(self._tmp, "data")
        self.service = CoreService(data_dir=self.data_dir)

    def tearDown(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _app_dir(self) -> str:
        d = os.path.join(self._tmp, "app")
        os.makedirs(os.path.join(d, "models"), exist_ok=True)
        os.makedirs(os.path.join(d, "adapter"), exist_ok=True)
        with open(os.path.join(d, "models", "model.pkl"), "wb") as fh:
            fh.write(b"dummy-model")
        with open(os.path.join(d, "adapter", "main.py"), "w", encoding="utf-8") as fh:
            fh.write("class Adapter:\n    pass\n")
        return d

    def _write_manifest(self, app_dir: str, manifest: dict) -> str:
        path = os.path.join(app_dir, "manifest.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh)
        # R6 (Day 3): valid-JSON manifests must be signed to reach the
        # validation/artifact checks (signature verified FIRST).
        sign_manifest(app_dir)
        return path

    def _assert_rejected(self, app_dir: str, manifest: dict, expect_reason: str) -> None:
        path = self._write_manifest(app_dir, manifest)
        ok, message, entry = self.service.install_app(path)
        self.assertFalse(ok, f"expected rejection, got ok with {message!r}")
        self.assertIn(expect_reason, message.lower())
        self.assertIsNone(entry)

    # 1. missing manifest
    def test_01_missing_manifest(self) -> None:
        app_dir = self._app_dir()
        path = self._write_manifest(app_dir, dict(VALID_MANIFEST))
        os.remove(path)
        ok, message, _ = self.service.install_app(path)
        self.assertFalse(ok)
        self.assertIn("manifest not found", message.lower())

    # 2. invalid JSON
    def test_02_invalid_json(self) -> None:
        app_dir = self._app_dir()
        path = os.path.join(app_dir, "manifest.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{ this is not json")
        ok, message, _ = self.service.install_app(path)
        self.assertFalse(ok)
        self.assertIn("invalid json", message.lower())

    # 3. invalid app_id (uppercase / spaces / too long)
    def test_03_invalid_app_id(self) -> None:
        app_dir = self._app_dir()
        m = copy.deepcopy(VALID_MANIFEST)
        m["app_id"] = "Bad_App ID"
        self._assert_rejected(app_dir, m, "app_id")

    # 4. unknown capability
    def test_04_unknown_capability(self) -> None:
        app_dir = self._app_dir()
        m = copy.deepcopy(VALID_MANIFEST)
        m["capabilities"] = ["inference", "teleport"]
        self._assert_rejected(app_dir, m, "unknown capability")

    # 5. missing adapter
    def test_05_missing_adapter(self) -> None:
        app_dir = self._app_dir()
        m = copy.deepcopy(VALID_MANIFEST)
        del m["adapter"]
        self._assert_rejected(app_dir, m, "missing required field: adapter")

    # 6. missing model
    def test_06_missing_model(self) -> None:
        app_dir = self._app_dir()
        m = copy.deepcopy(VALID_MANIFEST)
        m["models"] = []
        self._assert_rejected(app_dir, m, "models")

    # 7. incorrect SHA-256 (model file content does not match manifest hash)
    def test_07_incorrect_sha256(self) -> None:
        app_dir = self._app_dir()
        m = copy.deepcopy(VALID_MANIFEST)
        m["models"][0]["sha256"] = "f" * 64  # wrong hash
        self._assert_rejected(app_dir, m, "sha256 mismatch")

    # 8. undeclared model artifact (extra file in models/ not in manifest)
    def test_08_undeclared_model_artifact(self) -> None:
        app_dir = self._app_dir()
        # valid manifest, but a second model file exists undeclared
        with open(os.path.join(app_dir, "models", "extra.pkl"), "wb") as fh:
            fh.write(b"undeclared")
        m = copy.deepcopy(VALID_MANIFEST)
        # hash of declared model must match (real sha of "dummy-model")
        import hashlib
        real_sha = hashlib.sha256(b"dummy-model").hexdigest()
        m["models"][0]["sha256"] = real_sha
        m["adapter"]["files"][0]["sha256"] = hashlib.sha256(
            b"class Adapter:\n    pass\n"
        ).hexdigest()
        # install succeeds; orphan flag must be recorded in audit
        path = self._write_manifest(app_dir, m)
        ok, message, entry = self.service.install_app(path)
        self.assertTrue(ok, f"install should succeed; got {message!r}")
        # E4: orphan artifact flagged (audit entry), never silently ignored
        audit_entries = []
        audit_path = os.path.join(self.data_dir, "audit", "audit.jsonl")
        if os.path.exists(audit_path):
            with open(audit_path, "r", encoding="utf-8") as fh:
                for line in fh:
                    if line.strip():
                        audit_entries.append(json.loads(line))
        orphan_events = [
            e for e in audit_entries
            if e.get("action") == "orphan_artifact" or "orphan" in str(e.get("reason", "")).lower()
        ]
        self.assertTrue(
            orphan_events,
            "undeclared artifact must produce an orphan-flag audit event (E4)",
        )

    # 9. path traversal ../../file
    def test_09_path_traversal(self) -> None:
        app_dir = self._app_dir()
        m = copy.deepcopy(VALID_MANIFEST)
        m["models"][0]["file"] = "../../etc/passwd"
        self._assert_rejected(app_dir, m, "escapes app dir")

    # 10. unsupported model format
    def test_10_unsupported_model_format(self) -> None:
        app_dir = self._app_dir()
        m = copy.deepcopy(VALID_MANIFEST)
        m["models"][0]["format"] = "onnx"  # not in E1 whitelist
        self._assert_rejected(app_dir, m, "not in e1 whitelist")

    # 11. symlink escape (reviewer directive 19-08-2026; N2)
    def test_11_symlink_escape(self) -> None:
        """Artifact path syntactically inside the app directory -> the file
        is a symbolic link pointing OUTSIDE the app directory -> the
        installer MUST reject (N2 resolves filesystem symlinks via
        realpath before the containment decision)."""
        outside = os.path.join(self._tmp, "outside")
        os.makedirs(outside, exist_ok=True)
        outside_file = os.path.join(outside, "secret.pkl")
        with open(outside_file, "wb") as fh:
            fh.write(b"outside-secret-model")

        app_dir = os.path.join(self._tmp, "app")
        os.makedirs(os.path.join(app_dir, "models"), exist_ok=True)
        os.makedirs(os.path.join(app_dir, "adapter"), exist_ok=True)
        # symlink inside models/ whose target lives OUTSIDE app_dir
        os.symlink(outside_file, os.path.join(app_dir, "models", "model.pkl"))
        with open(os.path.join(app_dir, "adapter", "main.py"), "w", encoding="utf-8") as fh:
            fh.write("class Adapter:\n    pass\n")

        import hashlib

        m = copy.deepcopy(VALID_MANIFEST)
        m["models"][0]["sha256"] = hashlib.sha256(b"outside-secret-model").hexdigest()
        m["adapter"]["files"][0]["sha256"] = hashlib.sha256(
            b"class Adapter:\n    pass\n"
        ).hexdigest()
        self._assert_rejected(app_dir, m, "escapes app dir")

    # 12. signature path escape (reviewer finding 4, 21-08-2026)
    def test_12_signature_path_escape(self) -> None:
        """release.signature must obey the SAME N2 containment as model
        artifacts: the signature-file path is resolved with realpath and
        must remain strictly inside the app dir BEFORE it is opened.
        Sub-case (i): traversal string ../../<file>.
        Sub-case (ii): manifest.sig is a symlink pointing OUTSIDE."""
        import hashlib

        # sub-case (i): "../" traversal in release.signature
        outside = os.path.join(self._tmp, "outside.sig")
        with open(outside, "w", encoding="ascii") as fh:
            fh.write("AAAA")  # arbitrary bytes; must never be read

        app_dir = os.path.join(self._tmp, "app_trav")
        os.makedirs(os.path.join(app_dir, "models"), exist_ok=True)
        os.makedirs(os.path.join(app_dir, "adapter"), exist_ok=True)
        with open(os.path.join(app_dir, "models", "model.pkl"), "wb") as fh:
            fh.write(b"dummy-model")
        with open(os.path.join(app_dir, "adapter", "main.py"), "w", encoding="utf-8") as fh:
            fh.write("class Adapter:\n    pass\n")

        m = copy.deepcopy(VALID_MANIFEST)
        m["app_id"] = "sig-trav-app"
        m["models"][0]["sha256"] = hashlib.sha256(b"dummy-model").hexdigest()
        m["adapter"]["files"][0]["sha256"] = hashlib.sha256(
            b"class Adapter:\n    pass\n"
        ).hexdigest()
        m["release"] = {"key_id": "sydeco-test-key-v1", "signature": "../outside.sig"}
        path = os.path.join(app_dir, "manifest.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(m, fh)
        # signature is verified FIRST: the containment check runs before
        # the file is opened -> rejected even though the manifest JSON is
        # valid.
        ok, message, entry = self.service.install_app(path)
        self.assertFalse(ok, f"expected rejection, got ok with {message!r}")
        self.assertIn("escapes app dir", message.lower())
        self.assertIsNone(entry)

        # sub-case (ii): manifest.sig is a symlink pointing outside
        app_dir2 = os.path.join(self._tmp, "app_sym")
        os.makedirs(os.path.join(app_dir2, "models"), exist_ok=True)
        os.makedirs(os.path.join(app_dir2, "adapter"), exist_ok=True)
        with open(os.path.join(app_dir2, "models", "model.pkl"), "wb") as fh:
            fh.write(b"dummy-model")
        with open(os.path.join(app_dir2, "adapter", "main.py"), "w", encoding="utf-8") as fh:
            fh.write("class Adapter:\n    pass\n")
        os.symlink(outside, os.path.join(app_dir2, "manifest.sig"))

        m2 = copy.deepcopy(VALID_MANIFEST)
        m2["app_id"] = "sig-sym-app"
        m2["models"][0]["sha256"] = hashlib.sha256(b"dummy-model").hexdigest()
        m2["adapter"]["files"][0]["sha256"] = hashlib.sha256(
            b"class Adapter:\n    pass\n"
        ).hexdigest()
        path2 = os.path.join(app_dir2, "manifest.json")
        with open(path2, "w", encoding="utf-8") as fh:
            json.dump(m2, fh)
        ok2, message2, entry2 = self.service.install_app(path2)
        self.assertFalse(ok2, f"expected rejection, got ok with {message2!r}")
        self.assertIn("escapes app dir", message2.lower())
        self.assertIsNone(entry2)

    # 13. worker binds loopback ONLY (P6 / D2 req 3, 2026-08-24)
    def test_13_worker_binds_loopback_only(self) -> None:
        """The worker runtime must never listen on 0.0.0.0 / :: / an
        external interface (L3). The bind host is hard-coded to
        127.0.0.1; this test spawns the real worker_runtime process and
        inspects /proc/net/tcp for the listener address."""
        import http.client
        import socket
        import subprocess
        import sys
        import time

        from sydeco_lightml_core.worker_runtime import WORKER_BIND_HOST

        self.assertEqual(WORKER_BIND_HOST, "127.0.0.1")

        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()

        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        app_root = os.path.join(repo, "examples", "text-classifier")
        env = dict(
            os.environ,
            PYTHONPYCACHEPREFIX=tempfile.mkdtemp(prefix="sydeco-sec-pycache-"),
        )
        # Day 2 (P3): the worker fails closed without a credential — the
        # dev/test path passes a credential FILE (path on the command line,
        # secret never in argv/env). The systemd path uses LoadCredential=.
        cred_dir = tempfile.mkdtemp(prefix="sydeco-sec-cred-")
        cred_file = os.path.join(cred_dir, "worker-secret")
        with open(cred_file, "w", encoding="utf-8") as fh:
            fh.write("a" * 64)
        proc = subprocess.Popen(
            [sys.executable, "-m", "sydeco_lightml_core.worker_runtime",
             "--app-root", app_root, "--port", str(port),
             "--credential-file", cred_file],
            cwd=repo, env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            deadline = time.time() + 20
            ready = False
            while time.time() < deadline:
                if proc.poll() is not None:
                    break
                try:
                    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
                    # Day 2 (P4): /health/ready is AUTHENTICATED — the probe
                    # must present the worker credential
                    conn.request("GET", "/health/ready",
                                 headers={"Authorization": "Bearer " + "a" * 64})
                    resp = conn.getresponse()
                    resp.read()
                    conn.close()
                    if resp.status == 200:
                        ready = True
                        break
                except Exception:
                    pass
                time.sleep(0.2)
            self.assertTrue(ready, "worker did not become ready")
            with open("/proc/net/tcp", "r", encoding="utf-8") as fh:
                lines = fh.read().splitlines()
            hexport = f"{port:04X}"
            listeners = [ln for ln in lines if hexport in ln and " 0A " in ln]
            self.assertTrue(listeners, f"no listener found for port {port}")
            for ln in listeners:
                local = ln.split()[1].split(":")[0]
                self.assertEqual(
                    local, "0100007F",
                    f"worker listener is NOT loopback: 0x{local} (port {port})",
                )
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()


if __name__ == "__main__":
    unittest.main()
