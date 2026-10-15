import unittest

from pcb_dev.analysis import analyze_tolerances, thermal_estimate_c
from pcb_dev.model import Design


def design():
    return Design.from_dict({"schema_version": 1, "name": "divider", "version": "0.1.0",
        "board": {"width_mm": 10, "height_mm": 10}, "root": {"id": "board",
        "parameters": {"vin": 5, "r_top": 1000, "r_bottom": 1000},
        "equations": {"vout": "vin * r_bottom / (r_top + r_bottom)"},
        "constraints": [{"expression": "vout <= 2.6", "message": "Voltage upper limit"}]}})


class AnalysisTests(unittest.TestCase):
    def test_uniform_analysis_reproducible_and_original_unchanged(self):
        source = design()
        before = source.fingerprint()
        one = analyze_tolerances(source, "board", {"r_top": 0.1, "r_bottom": 0.1}, samples=30, seed=7)
        two = analyze_tolerances(source, "board", {"r_bottom": 0.1, "r_top": 0.1}, samples=30, seed=7)
        self.assertEqual(one, two)
        self.assertEqual(source.fingerprint(), before)
        self.assertAlmostEqual(one["outputs"]["vout"]["corner_min"], 2.25)
        self.assertAlmostEqual(one["outputs"]["vout"]["corner_max"], 2.75)
        self.assertGreater(one["failed_corners"], 0)
        self.assertFalse(one["hardware_validated"])

    def test_zero_tolerance_constant(self):
        result = analyze_tolerances(design(), "board", {"r_top": 0}, samples=5)
        self.assertEqual(result["outputs"]["vout"]["sample_stddev"], 0)
        self.assertEqual(result["failed_samples"], 0)

    def test_invalid_inputs_and_derived_parameters_rejected(self):
        for relative in [{}, {"vout": 0.1}, {"missing": 0.1}, {"r_top": -0.1},
                         {"r_top": float("nan")}, {"r_top": True}]:
            with self.subTest(relative=relative), self.assertRaises(ValueError):
                analyze_tolerances(design(), "board", relative, samples=3)
        with self.assertRaises(ValueError):
            analyze_tolerances(design(), "unknown", {"r_top": 0.1})

    def test_invalid_budget_rejected(self):
        for samples in [True, 0, 10001]:
            with self.assertRaises(ValueError):
                analyze_tolerances(design(), "board", {"r_top": 0.1}, samples=samples)

    def test_declared_thermal_resistance_calculation(self):
        self.assertAlmostEqual(thermal_estimate_c(0.3, 70, 25), 46)
        for values in [(1, 0, 25), (-1, 70, 25), (1, 70, -300), (1e308, 1e308, 25)]:
            with self.subTest(values=values), self.assertRaises(ValueError):
                thermal_estimate_c(*values)
