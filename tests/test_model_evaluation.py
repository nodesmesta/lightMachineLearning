import unittest

from tools.evaluation_dataset import DatasetRecord
from tools.model_evaluation import evaluate_predictions, evaluate_isolation_forest


class FakeIsolationForest:
    def __init__(self):
        self.fit_rows = None

    def fit(self, rows):
        self.fit_rows = list(rows)
        return self

    def predict_proba(self, rows):
        return [[0.9, 0.1] if row[0] < 0.5 else [0.1, 0.9] for row in rows]


class ModelEvaluationTests(unittest.TestCase):
    def test_metrics_include_confusion_matrix_and_rates(self):
        report = evaluate_predictions(
            labels=[0, 0, 1, 1],
            predictions=[0, 1, 1, 0],
            scores=[0.1, 0.8, 0.9, 0.2],
            threshold=0.75,
        )

        self.assertEqual(report["confusion_matrix"], {"tn": 1, "fp": 1, "fn": 1, "tp": 1})
        self.assertEqual(report["accuracy"], 0.5)
        self.assertEqual(report["balanced_accuracy"], 0.5)
        self.assertEqual(report["precision"], 0.5)
        self.assertEqual(report["recall"], 0.5)
        self.assertEqual(report["f1"], 0.5)
        self.assertEqual(report["false_positive_rate"], 0.5)
        self.assertEqual(report["false_negative_rate"], 0.5)

    def test_isolation_forest_trains_only_on_normal_training_records(self):
        records = [
            DatasetRecord((0.1,) + (0.0,) * 11, 0, "train-normal"),
            DatasetRecord((0.9,) + (0.0,) * 11, 1, "train-anomaly"),
            DatasetRecord((0.1,) + (0.0,) * 11, 0, "test-normal"),
            DatasetRecord((0.9,) + (0.0,) * 11, 1, "test-anomaly"),
        ]
        model = FakeIsolationForest()

        report = evaluate_isolation_forest(
            model,
            train_records=records[:2],
            evaluation_records=records[2:],
            threshold=0.75,
        )

        self.assertEqual(model.fit_rows, [[0.1] + [0.0] * 11])
        self.assertEqual(report["sample_count"], 2)
        self.assertEqual(report["class_counts"], {"normal": 1, "anomaly": 1})
        self.assertEqual(report["accuracy"], 1.0)
        self.assertEqual(report["threshold"], 0.75)

    def test_rejects_training_set_without_normal_records(self):
        records = [DatasetRecord((0.9,) + (0.0,) * 11, 1, "anomaly")]

        with self.assertRaises(ValueError):
            evaluate_isolation_forest(
                FakeIsolationForest(), records, records, threshold=0.75
            )


if __name__ == "__main__":
    unittest.main()
