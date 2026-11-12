import unittest

from tools.evaluation_dataset import DatasetRecord
from tools.train_socsoar_model import fit_candidate


class RecordingModel:
    def __init__(self):
        self.fit_rows = None

    def fit(self, rows):
        self.fit_rows = list(rows)
        return self


class TrainingPipelineTests(unittest.TestCase):
    def test_fits_only_normal_training_rows_with_runtime_preprocessing(self):
        records = [
            DatasetRecord((10.0,) + (0.0,) * 11, 0, "normal-1"),
            DatasetRecord((99.0,) + (0.0,) * 11, 1, "anomaly-1"),
            DatasetRecord((20.0,) + (0.0,) * 11, 0, "normal-2"),
        ]
        model = RecordingModel()

        result = fit_candidate(
            model,
            records,
            preprocess=lambda row: [row[0] / 10.0] + list(row[1:]),
        )

        self.assertIs(result, model)
        self.assertEqual(model.fit_rows, [[1.0] + [0.0] * 11, [2.0] + [0.0] * 11])

    def test_rejects_training_data_without_normal_records(self):
        records = [DatasetRecord((1.0,) + (0.0,) * 11, 1, "anomaly-1")]

        with self.assertRaises(ValueError):
            fit_candidate(RecordingModel(), records, preprocess=lambda row: row)


if __name__ == "__main__":
    unittest.main()
