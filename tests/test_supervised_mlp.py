import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
MODEL_DIR = REPO_ROOT / "bundles" / "socsoar-alert-scoring" / "models"
if str(MODEL_DIR) not in sys.path:
    sys.path.insert(0, str(MODEL_DIR))

from supervised_mlp import SupervisedMLP  # noqa: E402


class SupervisedMLPTests(unittest.TestCase):
    def _separable_data(self):
        normal = [[0.05 + index * 0.001] + [0.0] * 11 for index in range(8)]
        anomaly = [[0.95 - index * 0.001] + [0.0] * 11 for index in range(8)]
        return normal + anomaly, [0] * len(normal) + [1] * len(anomaly)

    def test_fit_predicts_separable_labels_with_bounded_scores(self):
        rows, labels = self._separable_data()
        model = SupervisedMLP(seed=7, epochs=300, hidden_dims=(8, 4))

        model.fit(rows, labels)
        scores = [row[1] for row in model.predict_proba(rows)]

        self.assertTrue(all(0.0 <= score <= 1.0 for score in scores))
        self.assertLess(sum(scores[:8]) / 8.0, 0.4)
        self.assertGreater(sum(scores[8:]) / 8.0, 0.6)

    def test_same_seed_produces_same_scores(self):
        rows, labels = self._separable_data()
        first = SupervisedMLP(seed=11, epochs=100, hidden_dims=(8, 4))
        second = SupervisedMLP(seed=11, epochs=100, hidden_dims=(8, 4))

        first.fit(rows, labels)
        second.fit(rows, labels)

        self.assertEqual(first.predict_proba(rows), second.predict_proba(rows))

    def test_rejects_wrong_feature_dimension(self):
        model = SupervisedMLP()

        with self.assertRaises(ValueError):
            model.fit([[0.0] * 11], [0])


if __name__ == "__main__":
    unittest.main()
