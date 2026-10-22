"""Canonical range regressions for both SOC/SOAR adapter input representations."""
import importlib.util
import json
import math
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "bundles" / "socsoar-alert-scoring"
MANIFEST = json.loads((BUNDLE / "manifest.json").read_text())
spec = importlib.util.spec_from_file_location("socsoar_contract_adapter", BUNDLE / "adapter" / "main.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
ORDER = MANIFEST["input_schema"]["properties"]["telemetry"]["required"]
BOUNDS = MANIFEST["input_schema"]["properties"]["telemetry"]["properties"]
VALID = dict(pid_ratio=.1, ppid_ratio=.01, cmd_len=16., arg_count=2.,
    token_entropy=2., uid_class=1., start_tick_delta=100., mem_rss_ratio=.01,
    cpu_tick_ratio=.002, fd_count=8., thread_count=1., privilege_flag=0.)

class RecordingModel:
    def __init__(self):
        self.calls = 0

    def predict_proba(self, vectors):
        self.calls += 1
        return [[.25, .75]]

class SocsoarInputContractTests(unittest.TestCase):
    def setUp(self):
        self.model = RecordingModel()
        self.adapter = module.Adapter()
        self.adapter.initialize({"models": {"primary": self.model}, "config": MANIFEST})

    def payload(self, telemetry, representation):
        return {representation: telemetry if representation == "telemetry"
                else [telemetry[key] for key in ORDER]}

    def test_outside_every_bound_is_rejected_before_prediction(self):
        for key in ORDER:
            for bound, delta in (("minimum", -1.), ("maximum", 1.)):
                if bound not in BOUNDS[key]:
                    continue
                for representation in ("telemetry", "features"):
                    with self.subTest(key=key, bound=bound, representation=representation):
                        telemetry = dict(VALID, **{key: BOUNDS[key][bound] + delta})
                        before = self.model.calls
                        result = self.adapter.infer(self.payload(telemetry, representation), {})
                        self.assertEqual(result["status"], "error")
                        self.assertEqual(result["http_status"], 400)
                        self.assertIsNone(result["anomaly_score"])
                        self.assertIsNone(result["confidence"])
                        self.assertEqual(self.model.calls, before)

    def test_exact_bounds_remain_valid_in_both_representations(self):
        for key in ORDER:
            for bound in ("minimum", "maximum"):
                if bound not in BOUNDS[key]:
                    continue
                for representation in ("telemetry", "features"):
                    with self.subTest(key=key, bound=bound, representation=representation):
                        telemetry = dict(VALID, **{key: BOUNDS[key][bound]})
                        result = self.adapter.infer(self.payload(telemetry, representation), {})
                        self.assertEqual(result["status"], "ok")
                        self.assertEqual(result["anomaly_score"], .75)

    def test_invalid_values_remain_rejected_without_prediction(self):
        for value in (True, "bad", math.nan, math.inf, 10 ** 400):
            for representation in ("telemetry", "features"):
                with self.subTest(value=value, representation=representation):
                    telemetry = dict(VALID, pid_ratio=value)
                    result = self.adapter.infer(self.payload(telemetry, representation), {})
                    self.assertEqual(result["http_status"], 400)
                    self.assertIsNone(result["anomaly_score"])
                    self.assertEqual(self.model.calls, 0)

if __name__ == "__main__":
    unittest.main()
