import pickle
import sys
import tempfile
import unittest
from pathlib import Path

from tools.evaluate_socsoar_model import _load_model


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


if __name__ == "__main__":
    unittest.main()
