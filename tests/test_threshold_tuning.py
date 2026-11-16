import unittest

from tools.evaluation_dataset import DatasetRecord
from tools.model_evaluation import tune_threshold


class ThresholdTuningTests(unittest.TestCase):
    def test_selects_best_threshold_using_validation_only(self):
        class ScoringModel:
            def fit(self, rows):
                self.fit_rows = rows
                return self

            def predict_proba(self, rows):
                return [[1.0 - row[0], row[0]] for row in rows]

        train = [DatasetRecord((0.1,) + (0.0,) * 11, 0, "train")]
        validation = [
            DatasetRecord((0.1,) + (0.0,) * 11, 0, "v-normal-1"),
            DatasetRecord((0.2,) + (0.0,) * 11, 0, "v-normal-2"),
            DatasetRecord((0.8,) + (0.0,) * 11, 1, "v-anomaly-1"),
            DatasetRecord((0.9,) + (0.0,) * 11, 1, "v-anomaly-2"),
        ]

        result = tune_threshold(
            ScoringModel(), train, validation, thresholds=(0.15, 0.5, 0.85)
        )

        self.assertEqual(result["threshold"], 0.5)
        self.assertEqual(result["report"]["accuracy"], 1.0)
        self.assertEqual(result["selection_set"], "validation")

    def test_applies_preprocessing_before_threshold_selection(self):
        class ScoringModel:
            def fit(self, rows):
                self.fit_rows = rows
                return self

            def predict_proba(self, rows):
                return [[1.0 - row[0], row[0]] for row in rows]

        train = [DatasetRecord((10.0,) + (0.0,) * 11, 0, "train")]
        validation = [
            DatasetRecord((10.0,) + (0.0,) * 11, 0, "v-normal"),
            DatasetRecord((90.0,) + (0.0,) * 11, 1, "v-anomaly"),
        ]

        result = tune_threshold(
            ScoringModel(),
            train,
            validation,
            thresholds=(0.5,),
            preprocess=lambda row: [row[0] / 100.0] + list(row[1:]),
        )

        self.assertEqual(result["report"]["accuracy"], 1.0)

        with self.assertRaises(ValueError):
            tune_threshold(object(), [], [], thresholds=())


if __name__ == "__main__":
    unittest.main()
