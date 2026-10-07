"""Multi-Application Universal Compatibility & Zero-Regression Test Suite (P3).

Proves Acceptance Gate P1:
Demonstrates that LightML Core concurrently hosts three completely independent
applications from distinct domains on the exact same Core engine without
modifying Core source code:
  1. Zero-Code Standalone ML App (via DefaultModelAdapter)
  2. Document Intelligence App (PrivateDocsAI custom adapter)
  3. Network Telemetry & Risk Scoring App (MiniFW-AI risk adapter)
"""
from __future__ import annotations

import json
import os
import pickle
import shutil
import tempfile
import unittest

import sydeco_lightml_core.core
from sydeco_lightml_core.core import CoreService, _sha256_file
from sydeco_lightml_core.adapter import Adapter
from sydeco_lightml_core.keys import sign_manifest_canonical

DEV_KEY_PATH = os.path.join(
    os.path.dirname(__file__), "..", "devkeys", "DEVELOPMENT_TEST_KEY_DO_NOT_USE_IN_PRODUCTION.pem"
)
DEV_PUB_KEY_PATH = os.path.join(
    os.path.dirname(__file__), "..", "devkeys", "DEVELOPMENT_TEST_KEY_DO_NOT_USE_IN_PRODUCTION.pub.pem"
)


class MockTabularModel:
    """Mock model for App 1 (Zero-code tabular classifier)."""
    def predict(self, X):
        return [1 if x > 0.5 else 0 for x in X]


class TestMultiAppCompatibility(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp(prefix="test_multiapp_")
        self.data_dir = os.path.join(self.temp_dir, "data")
        os.makedirs(self.data_dir, exist_ok=True)
        with open(DEV_PUB_KEY_PATH, "r", encoding="ascii") as fh:
            pub_pem = fh.read()
        with open(os.path.join(self.data_dir, "trusted_keys.json"), "w", encoding="utf-8") as fh:
            json.dump({"sydeco-test-key-v1": pub_pem}, fh)
        self.service = CoreService(data_dir=self.data_dir, worker_mode="inprocess")

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    # -------------------------------------------------------------------------
    # Bundle Builders
    # -------------------------------------------------------------------------

    def _create_app1_zero_code(self) -> str:
        """App 1: Standalone model with NO adapter/ directory (Zero-Code)."""
        app_dir = os.path.join(self.temp_dir, "app1_zero_code")
        os.makedirs(os.path.join(app_dir, "models"), exist_ok=True)

        model_path = os.path.join(app_dir, "models", "tabular_model.pkl")
        with open(model_path, "wb") as fh:
            pickle.dump(MockTabularModel(), fh)
        model_sha = _sha256_file(model_path)

        manifest = {
            "manifest_version": 1,
            "app_id": "zero-code-model",
            "name": "Zero-Code Tabular Classifier",
            "version": "1.0.0",
            "capabilities": ["inference"],
            "models": [
                {
                    "role": "primary",
                    "file": "models/tabular_model.pkl",
                    "format": "pickle",
                    "sha256": model_sha,
                }
            ],
            "release": {"key_id": "sydeco-test-key-v1", "signature": "manifest.sig"},
            "input_schema": {"type": "object"},
            "output_schema": {"type": "object"},
            "permissions": {"network": "none"},
            "api": {"authentication": "token"},
            "resource_limits": {
                "max_memory": 268435456,
                "max_cpu": 50,
                "inference_timeout": 30,
                "concurrency": 1,
            },
        }
        with open(os.path.join(app_dir, "manifest.json"), "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=2)
        with open(DEV_KEY_PATH, "r", encoding="ascii") as fh:
            priv_pem = fh.read()
        sig_b64 = sign_manifest_canonical(manifest, priv_pem)
        with open(os.path.join(app_dir, "manifest.sig"), "w", encoding="ascii") as fh:
            fh.write(sig_b64 + "\n")
        return app_dir

    def _create_app2_privatedocs(self) -> str:
        """App 2: Document intelligence application with custom adapter."""
        app_dir = os.path.join(self.temp_dir, "app2_privatedocs")
        os.makedirs(os.path.join(app_dir, "models"), exist_ok=True)
        os.makedirs(os.path.join(app_dir, "adapter"), exist_ok=True)

        rule_path = os.path.join(app_dir, "models", "doc_rules.pkl")
        with open(rule_path, "wb") as fh:
            pickle.dump({"keywords": ["confidential", "secret", "contract"]}, fh)
        rule_sha = _sha256_file(rule_path)

        adapter_code = """
from sydeco_lightml_core.adapter import Adapter

class Adapter(Adapter):
    def initialize(self, context):
        self.rules = context["models"].get("rules", {})

    def infer(self, request, context):
        query = request.get("query", "")
        keywords = self.rules.get("keywords", [])
        matches = [kw for kw in keywords if kw in query.lower()]
        return {
            "status": "ok",
            "operation": request.get("operation", "search"),
            "matches": matches,
            "match_count": len(matches),
        }

    def shutdown(self):
        pass
"""
        adapter_path = os.path.join(app_dir, "adapter", "main.py")
        with open(adapter_path, "w", encoding="utf-8") as fh:
            fh.write(adapter_code)
        adapter_sha = _sha256_file(adapter_path)

        manifest = {
            "manifest_version": 1,
            "app_id": "privatedocs-app",
            "name": "SYDECO PrivateDocs AI",
            "version": "1.0.0",
            "capabilities": ["inference"],
            "models": [
                {
                    "role": "rules",
                    "file": "models/doc_rules.pkl",
                    "format": "pickle",
                    "sha256": rule_sha,
                }
            ],
            "adapter": {
                "entry": "adapter/main.py",
                "files": [{"file": "adapter/main.py", "sha256": adapter_sha}],
            },
            "release": {"key_id": "sydeco-test-key-v1", "signature": "manifest.sig"},
            "input_schema": {"type": "object"},
            "output_schema": {"type": "object"},
            "permissions": {"network": "none"},
            "api": {"authentication": "token"},
            "resource_limits": {
                "max_memory": 268435456,
                "max_cpu": 50,
                "inference_timeout": 30,
                "concurrency": 1,
            },
        }
        with open(os.path.join(app_dir, "manifest.json"), "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=2)
        with open(DEV_KEY_PATH, "r", encoding="ascii") as fh:
            priv_pem = fh.read()
        sig_b64 = sign_manifest_canonical(manifest, priv_pem)
        with open(os.path.join(app_dir, "manifest.sig"), "w", encoding="ascii") as fh:
            fh.write(sig_b64 + "\n")
        return app_dir

    def _create_app3_minifw_ai(self) -> str:
        """App 3: MiniFW-AI network telemetry recommendation adapter."""
        app_dir = os.path.join(self.temp_dir, "app3_minifw")
        os.makedirs(os.path.join(app_dir, "models"), exist_ok=True)
        os.makedirs(os.path.join(app_dir, "adapter"), exist_ok=True)

        model_path = os.path.join(app_dir, "models", "threat_scoring.pkl")
        with open(model_path, "wb") as fh:
            pickle.dump({"malicious_domains": ["c2-bot.test", "malware.onion"]}, fh)
        model_sha = _sha256_file(model_path)

        adapter_code = """
from sydeco_lightml_core.adapter import Adapter

class Adapter(Adapter):
    def initialize(self, context):
        self.db = context["models"].get("threat_scoring", {})

    def infer(self, request, context):
        dns_query = request.get("dns_query", "")
        dest_port = request.get("dest_port", 80)
        malicious = self.db.get("malicious_domains", [])

        if any(d in dns_query for d in malicious) or dest_port == 6667:
            risk_score = 0.95
            risk_class = "HIGH"
            recommendation = "BLOCK"
        else:
            risk_score = 0.05
            risk_class = "LOW"
            recommendation = "ALLOW"

        return {
            "application_id": "minifw_ai",
            "request_id": context.get("request_id", "req-001"),
            "result": {
                "risk_score": risk_score,
                "risk_class": risk_class,
                "recommendation": recommendation,
            },
            "confidence": 0.98,
            "model_version": "1.0.0",
            "ruleset_version": "2026.09",
            "limitations": [],
            "evidence_refs": [f"query:{dns_query}"],
        }

    def shutdown(self):
        pass
"""
        adapter_path = os.path.join(app_dir, "adapter", "main.py")
        with open(adapter_path, "w", encoding="utf-8") as fh:
            fh.write(adapter_code)
        adapter_sha = _sha256_file(adapter_path)

        manifest = {
            "manifest_version": 1,
            "app_id": "minifw-ai",
            "name": "SYDECO MiniFW-AI Risk Engine",
            "version": "1.0.0",
            "capabilities": ["inference"],
            "models": [
                {
                    "role": "threat_scoring",
                    "file": "models/threat_scoring.pkl",
                    "format": "pickle",
                    "sha256": model_sha,
                }
            ],
            "adapter": {
                "entry": "adapter/main.py",
                "files": [{"file": "adapter/main.py", "sha256": adapter_sha}],
            },
            "release": {"key_id": "sydeco-test-key-v1", "signature": "manifest.sig"},
            "input_schema": {"type": "object"},
            "output_schema": {"type": "object"},
            "permissions": {"network": "none"},
            "api": {"authentication": "token"},
            "resource_limits": {
                "max_memory": 268435456,
                "max_cpu": 50,
                "inference_timeout": 30,
                "concurrency": 1,
            },
        }
        with open(os.path.join(app_dir, "manifest.json"), "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=2)
        with open(DEV_KEY_PATH, "r", encoding="ascii") as fh:
            priv_pem = fh.read()
        sig_b64 = sign_manifest_canonical(manifest, priv_pem)
        with open(os.path.join(app_dir, "manifest.sig"), "w", encoding="ascii") as fh:
            fh.write(sig_b64 + "\n")
        return app_dir

    # -------------------------------------------------------------------------
    # Multi-App Regression & Compatibility Tests
    # -------------------------------------------------------------------------

    def test_concurrent_multi_app_compatibility_and_zero_regression(self) -> None:
        """Sequential installation and continuous cross-application regression test."""
        # 1. Setup bundle directories
        dir_app1 = self._create_app1_zero_code()
        dir_app2 = self._create_app2_privatedocs()
        dir_app3 = self._create_app3_minifw_ai()

        # Step 1: Install and run App 1 (Zero-Code)
        ok1, msg1, _ = self.service.install_app(
            os.path.join(dir_app1, "manifest.json"), app_root=dir_app1
        )
        self.assertTrue(ok1, f"App 1 install failed: {msg1}")
        s_ok1, _, _ = self.service.start_app("zero-code-model")
        self.assertTrue(s_ok1)

        # Infer App 1
        host1 = self.service.worker_host("zero-code-model")
        res1 = host1.adapter.infer({"inputs": [0.1, 0.9]}, {})
        self.assertEqual(res1["status"], "ok")
        self.assertEqual(res1["result"], [0, 1])

        # Step 2: Install and run App 2 (PrivateDocs)
        ok2, msg2, _ = self.service.install_app(
            os.path.join(dir_app2, "manifest.json"), app_root=dir_app2
        )
        self.assertTrue(ok2, f"App 2 install failed: {msg2}")
        s_ok2, _, _ = self.service.start_app("privatedocs-app")
        self.assertTrue(s_ok2)

        # Infer App 2
        host2 = self.service.worker_host("privatedocs-app")
        res2 = host2.adapter.infer({"query": "Check confidential agreement"}, {})
        self.assertEqual(res2["status"], "ok")
        self.assertEqual(res2["matches"], ["confidential"])

        # REGRESSION CHECK: App 1 still functional after App 2 installed
        res1_again = host1.adapter.infer({"inputs": [0.8, 0.2]}, {})
        self.assertEqual(res1_again["result"], [1, 0])

        # Step 3: Install and run App 3 (MiniFW-AI)
        ok3, msg3, _ = self.service.install_app(
            os.path.join(dir_app3, "manifest.json"), app_root=dir_app3
        )
        self.assertTrue(ok3, f"App 3 install failed: {msg3}")
        s_ok3, _, _ = self.service.start_app("minifw-ai")
        self.assertTrue(s_ok3)

        # Infer App 3: Malicious query -> BLOCK
        host3 = self.service.worker_host("minifw-ai")
        res3_block = host3.adapter.infer({"dns_query": "connect to c2-bot.test domain"}, {})
        self.assertEqual(res3_block["result"]["recommendation"], "BLOCK")
        self.assertEqual(res3_block["result"]["risk_class"], "HIGH")

        # Infer App 3: Benign query -> ALLOW
        res3_allow = host3.adapter.infer({"dns_query": "normal-site.com", "dest_port": 443}, {})
        self.assertEqual(res3_allow["result"]["recommendation"], "ALLOW")
        self.assertEqual(res3_allow["result"]["risk_class"], "LOW")

        # REGRESSION CHECK: App 1 and App 2 still functional after App 3 installed
        res1_final = host1.adapter.infer({"inputs": [0.1, 0.2]}, {})
        self.assertEqual(res1_final["result"], [0, 0])

        res2_final = host2.adapter.infer({"query": "Contract secret details"}, {})
        self.assertEqual(res2_final["match_count"], 2)

        # Verify all 3 applications coexist in the Registry
        registered_apps = self.service.registry.list_apps()
        app_ids = {a["app_id"] for a in registered_apps}
        self.assertEqual(app_ids, {"zero-code-model", "privatedocs-app", "minifw-ai"})


if __name__ == "__main__":
    unittest.main()
