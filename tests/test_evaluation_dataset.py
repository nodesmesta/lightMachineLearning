import csv
import json
import tempfile
import unittest
from pathlib import Path

from tools.evaluation_dataset import (
    FEATURE_NAMES,
    DatasetFormatError,
    load_dataset,
    split_by_group,
)


class EvaluationDatasetTests(unittest.TestCase):
    def write_csv(self, rows):
        handle = tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False, newline="")
        self.addCleanup(lambda: Path(handle.name).unlink(missing_ok=True))
        fieldnames = [*FEATURE_NAMES, "label", "group_id"]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
        handle.close()
        return Path(handle.name)

    def base_row(self, group_id="host-a", label="0"):
        row = {name: "0.1" for name in FEATURE_NAMES}
        row.update(label=label, group_id=group_id)
        return row

    def test_loads_valid_csv_with_required_schema(self):
        path = self.write_csv([self.base_row()])

        dataset = load_dataset(path)

        self.assertEqual(len(dataset), 1)
        self.assertEqual(dataset[0].label, 0)
        self.assertEqual(dataset[0].group_id, "host-a")
        self.assertEqual(len(dataset[0].features), 12)

    def test_rejects_missing_group_column(self):
        path = self.write_csv([self.base_row()])
        text = path.read_text()
        path.write_text(text.replace(",group_id\n", "\n").replace(",host-a\n", "\n"))

        with self.assertRaises(DatasetFormatError):
            load_dataset(path)

    def test_rejects_invalid_label_and_nonfinite_feature(self):
        invalid_label = self.base_row(label="2")
        path = self.write_csv([invalid_label])
        with self.assertRaises(DatasetFormatError):
            load_dataset(path)

        nonfinite = self.base_row()
        nonfinite["pid_ratio"] = "nan"
        path = self.write_csv([nonfinite])
        with self.assertRaises(DatasetFormatError):
            load_dataset(path)

    def test_jsonl_has_same_schema_rules(self):
        handle = tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False)
        self.addCleanup(lambda: Path(handle.name).unlink(missing_ok=True))
        handle.write(json.dumps(self.base_row(group_id="host-b")) + "\n")
        handle.close()

        dataset = load_dataset(Path(handle.name))

        self.assertEqual(dataset[0].group_id, "host-b")

    def test_group_split_has_no_group_overlap_and_is_deterministic(self):
        records = []
        for group in ("a", "b", "c", "d", "e"):
            for label in (0, 1):
                row = self.base_row(group_id=group, label=str(label))
                records.extend(load_dataset(self.write_csv([row])))

        first = split_by_group(records, seed=7)
        second = split_by_group(records, seed=7)

        self.assertEqual(first, second)
        groups = [{record.group_id for record in part} for part in first]
        self.assertTrue(groups[0].isdisjoint(groups[1]))
        self.assertTrue(groups[0].isdisjoint(groups[2]))
        self.assertTrue(groups[1].isdisjoint(groups[2]))
        self.assertEqual(sum(map(len, first)), len(records))

    def test_group_split_preserves_both_classes_when_groups_allow_it(self):
        records = []
        for label in (0, 1):
            for group in range(3):
                row = self.base_row(group_id=f"{label}-{group}", label=str(label))
                records.extend(load_dataset(self.write_csv([row])))

        train, validation, test = split_by_group(records, seed=11)

        for split in (train, validation, test):
            self.assertEqual({record.label for record in split}, {0, 1})

    def test_group_split_keeps_train_as_the_largest_partition(self):
        records = []
        for label in (0, 1):
            for group in range(12):
                records.extend(
                    load_dataset(
                        self.write_csv(
                            [self.base_row(group_id=f"{label}-{group}", label=str(label))]
                        )
                    )
                )

        train, validation, test = split_by_group(records, seed=42)

        self.assertGreaterEqual(len(train), len(validation))
        self.assertGreaterEqual(len(train), len(test))


if __name__ == "__main__":
    unittest.main()
