"""Development verification-key provisioning for the documented daemon launcher."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]

class DaemonKeyProvisioningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        spec = importlib.util.spec_from_file_location("candidate_daemon_launcher", ROOT / "scripts/run_daemon.py")
        self.launcher = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.launcher)
        self.launcher.DATA_DIR = Path(self.tmp.name) / "data"
        self.launcher.DEV_PUB_KEY = Path(self.tmp.name) / "development.pub.pem"
        self.service = Mock()
        manifest = json.loads(self.launcher.SOCSOAR_MANIFEST.read_text())
        version = manifest["version"]
        self.service.registry.get.return_value = {"app_id": "socsoar-alert-scoring", "active_version": version, "versions": {version: {"manifest": manifest}}}
        self.service.start_app.return_value = (True, "started", None)

    def setup_and_read_key(self):
        with patch.object(self.launcher, "CoreService", return_value=self.service):
            with patch.dict(os.environ, {"SYDECO_LIGHTML_BEARER_TOKEN": ""}):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.launcher.setup_service()
        return json.loads((self.launcher.DATA_DIR / "trusted_keys.json").read_text())["sydeco-test-key-v1"]

    def test_missing_packaged_key_uses_exact_embedded_development_public_key(self):
        from sydeco_lightml_core.keys import DEFAULT_DEV_PUBKEY_PEM
        self.assertFalse(self.launcher.DEV_PUB_KEY.exists())
        self.assertEqual(self.setup_and_read_key(), DEFAULT_DEV_PUBKEY_PEM)

    def test_existing_key_file_is_preserved(self):
        self.launcher.DEV_PUB_KEY.write_text("existing-public-verification-key\n")
        self.assertEqual(self.setup_and_read_key(), "existing-public-verification-key\n")

if __name__ == "__main__":
    unittest.main()
