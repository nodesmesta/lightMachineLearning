import pickle
import sys
import tempfile
import unittest
from pathlib import Path
from typing import cast

from tools.evaluate_socsoar_model import _load_model
from sydeco_lightml_core.loader import load_artifacts


REPO_ROOT = Path(__file__).resolve().parents[1]
MODEL_DIR = REPO_ROOT / "bundles" / "socsoar-alert-scoring" / "models"


class ExternalModelLoadingTests(unittest.TestCase):
    def test_loads_candidate_from_clean_external_directory(self):
        sys.path.insert(0, str(MODEL_DIR))
        from endpoint_model import EndpointIsolationForest

        model = EndpointIsolationForest()
        with tempfile.TemporaryDirectory() as temp_dir:
            candidate_path = Path(temp_dir) / "candidate-model.pkl"
            with candidate_path.open("wb") as handle:
                pickle.dump(model, handle, protocol=pickle.HIGHEST_PROTOCOL)

            sys.path.remove(str(MODEL_DIR))
            sys.modules.pop("endpoint_model", None)
            loaded = _load_model(candidate_path)

        self.assertEqual(type(loaded).__module__, "endpoint_model")
        self.assertEqual(type(loaded).__name__, "EndpointIsolationForest")

    def test_bundle_loader_resolves_model_class_from_canonical_models_directory(self):
        manifest_path = REPO_ROOT / "bundles" / "socsoar-alert-scoring" / "manifest.json"
        bundle_root = manifest_path.parent
        manifest = __import__("json").loads(manifest_path.read_text(encoding="utf-8"))

        sys.modules.pop("endpoint_model", None)
        load_artifacts(manifest, str(bundle_root))

        module = sys.modules["endpoint_model"]
        self.assertEqual(
            Path(cast(str, module.__file__)).resolve(),
            (MODEL_DIR / "endpoint_model.py").resolve(),
        )

if __name__ == "__main__":
    unittest.main()
