import tempfile
import unittest
from pathlib import Path

from tools.evaluation_dataset import DatasetRecord
from tools.train_supervised_mlp import fit_candidate


class SupervisedTrainingTests(unittest.TestCase):
    def test_fit_candidate_uses_reviewed_labels_and_runtime_preprocessing(self):
        records = [
            DatasetRecord((0.0,) + (0.0,) * 11, 0, 'normal-1'),
            DatasetRecord((1.0,) + (0.0,) * 11, 1, 'anomaly-1'),
            DatasetRecord((0.1,) + (0.0,) * 11, 0, 'normal-2'),
            DatasetRecord((0.9,) + (0.0,) * 11, 1, 'anomaly-2'),
        ]
        model = fit_candidate(records, epochs=5, hidden_dims=(4, 2))

        self.assertTrue(model.fitted_)
        self.assertEqual(model.n_features_in_, 12)

    def test_fit_candidate_rejects_single_class_training_data(self):
        records = [DatasetRecord((0.0,) + (0.0,) * 11, 0, 'normal-1')]

        with self.assertRaises(ValueError):
            fit_candidate(records, epochs=5, hidden_dims=(4, 2))


if __name__ == '__main__':
    unittest.main()
