"""Unit tests for DefaultModelAdapter and zero-code manifest support (P1)."""
from __future__ import annotations

import unittest
from typing import Any, Dict

from sydeco_lightml_core.adapter import DefaultModelAdapter
from sydeco_lightml_core.manifest import REQUIRED_MANIFEST_FIELDS
from sydeco_lightml_core.validation import validate_manifest
from sydeco_lightml_core.worker_runtime import load_adapter


class MockModelPredict:
    def predict(self, X: Any) -> Any:
        return [1 if x > 0.5 else 0 for x in X]


class MockModelPredictProba:
    def predict_proba(self, X: Any) -> Any:
        return [[1.0 - x, x] for x in X]


class TestDefaultModelAdapter(unittest.TestCase):
    def test_required_fields_omits_adapter(self) -> None:
        self.assertNotIn("adapter", REQUIRED_MANIFEST_FIELDS)

    def test_validate_manifest_without_adapter(self) -> None:
        manifest = {
            "manifest_version": 1,
            "app_id": "zero-code-model",
            "name": "Zero Code App",
            "version": "1.0.0",
            "capabilities": ["inference"],
            "models": [
                {"role": "primary", "file": "models/model.pkl", "format": "pickle", "sha256": "a" * 64}
            ],
            "release": {
                "key_id": "sydeco-release-v1",
                "signature": "manifest.sig",
            },
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
        ok, errors = validate_manifest(manifest)
        self.assertTrue(ok, f"Validation failed: {errors}")

    def test_load_adapter_fallback(self) -> None:
        manifest = {
            "app_id": "no-adapter-app",
            "name": "No Adapter App",
            "version": "1.0.0",
        }
        adapter = load_adapter(manifest, "/nonexistent")
        self.assertIsInstance(adapter, DefaultModelAdapter)

    def test_default_adapter_lifecycle_predict(self) -> None:
        adapter = DefaultModelAdapter()
        context = {"models": {"primary": MockModelPredict()}}
        adapter.initialize(context)

        # Infer with inputs key
        res = adapter.infer({"inputs": [0.1, 0.9]}, context)
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["result"], [0, 1])
        self.assertEqual(res["model_role"], "primary")

        adapter.shutdown()

    def test_default_adapter_lifecycle_predict_proba(self) -> None:
        adapter = DefaultModelAdapter()
        context = {"models": {"classifier": MockModelPredictProba()}}
        adapter.initialize(context)

        res = adapter.infer({"features": [0.3]}, context)
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["result"], [[0.7, 0.3]])
        self.assertEqual(res["model_role"], "classifier")

        adapter.shutdown()


if __name__ == "__main__":
    unittest.main()
