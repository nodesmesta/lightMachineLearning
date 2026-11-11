import unittest

from tools.evaluation_dataset import DatasetRecord
from tools.phase4_heldout import HeldoutDatasetError, evaluate_heldout


class Phase4HeldoutTests(unittest.TestCase):
    def records(self):
        train = [DatasetRecord((0.1,) + (0.0,) * 11, 0, "train")]
        validation = [
            DatasetRecord((0.2,) + (0.0,) * 11, 0, "validation-normal"),
            DatasetRecord((0.8,) + (0.0,) * 11, 1, "validation-anomaly"),
        ]
        test = [
            DatasetRecord((0.1,) + (0.0,) * 11, 0, "test-normal"),
            DatasetRecord((0.9,) + (0.0,) * 11, 1, "test-anomaly"),
        ]
        return train, validation, test

    def test_heldout_gate_passes_and_marks_test_as_final_set(self):
        class Model:
            def fit(self, rows):
                return self

            def predict_proba(self, rows):
                return [[1.0 - row[0], row[0]] for row in rows]

        report = evaluate_heldout(*self.records(), model=Model(), threshold=0.5)

        self.assertEqual(report["phase"], 4)
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report["selection_set"], "frozen")
        self.assertEqual(report["test"]["accuracy"], 1.0)
        self.assertTrue(report["quality_gate"]["passed"])

    def test_rejects_group_overlap_before_scoring(self):
        train, validation, test = self.records()
        test[0] = DatasetRecord(test[0].features, 0, "train")

        with self.assertRaises(HeldoutDatasetError):
            evaluate_heldout(
                train,
                validation,
                test,
                model=object(),
                threshold=0.5,
            )

    def test_rejects_single_class_test_set(self):
        train, validation, _test = self.records()
        test = [DatasetRecord((0.1,) + (0.0,) * 11, 0, "test-only-normal")]

        with self.assertRaises(HeldoutDatasetError):
            evaluate_heldout(
                train,
                validation,
                test,
                model=object(),
                threshold=0.5,
            )


if __name__ == "__main__":
    unittest.main()
