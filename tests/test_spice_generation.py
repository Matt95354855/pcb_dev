"""Explicit SPICE model generation, including injection and omission failures."""

import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from pcb_dev.backends import SpiceError, generate_spice
from pcb_dev.backends.simulation import _validate_netlist
from pcb_dev.checks import check_design
from pcb_dev.model import load_design

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


class SpiceGenerationTests(unittest.TestCase):
    def test_nominal_fixtures_pass_preflight_and_generate_all_declared_analyses(self):
        for filename in ("rc_filter.json", "voltage_divider.json"):
            design = load_design(EXAMPLES / filename)
            self.assertFalse(check_design(design).blocking, filename)
            analyses = design.metadata["simulation"]["analyses"]
            with tempfile.TemporaryDirectory() as directory:
                for name, analysis in analyses.items():
                    with self.subTest(fixture=filename, analysis=name):
                        result = generate_spice(design, analysis, Path(directory) / f"{name}.cir")
                        self.assertEqual(result["status"], "generated")
                        self.assertFalse(result["executed"])
                        self.assertFalse(result["hardware_validated"])
                        self.assertEqual(result["net_map"]["GND"], "0")
                        self.assertEqual(len(result["models"]), 3)
                        text = Path(result["path"]).read_text()
                        _validate_netlist(text)
                        self.assertIn(".end\n", text)
                        self.assertEqual(result["output_sha256"], hashlib.sha256(text.encode()).hexdigest())
                        self.assertEqual(json.loads(Path(result["manifest_path"]).read_text())["output_sha256"], result["output_sha256"])

    def test_netlist_is_deterministic_and_preserves_pin_order(self):
        design = load_design(EXAMPLES / "voltage_divider.json")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = generate_spice(design, {"kind": "op"}, root / "a" / "out.cir")
            second = generate_spice(design.to_dict(), {"kind": "op"}, root / "b" / "out.cir")
            self.assertEqual(Path(first["path"]).read_bytes(), Path(second["path"]).read_bytes())
            self.assertEqual(Path(first["manifest_path"]).read_bytes(), Path(second["manifest_path"]).read_bytes())
            supply = first["reference_map"]["J1"]
            self.assertIn(f'{supply} {first["net_map"]["VIN"]} 0 DC 5.0', Path(first["path"]).read_text())
            self.assertEqual(next(model for model in first["models"] if model["reference"] == "R2")["value_si"], 10000)

    def test_missing_or_active_models_fail_without_partial_files(self):
        for mutation in (lambda data: data["root"]["components"][0]["parameters"].pop("spice"),
                         lambda data: data["root"]["components"][0]["parameters"]["spice"].update(kind="MCU"),
                         lambda data: data["root"]["components"][0]["parameters"]["spice"].update(kind="D")):
            data = load_design(EXAMPLES / "voltage_divider.json").to_dict()
            mutation(data)
            with tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(SpiceError):
                    generate_spice(data, {"kind": "op"}, Path(directory) / "out.cir")
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_unmodeled_physical_pins_and_unresolved_mapping_fail(self):
        for mapping in (["1", "missing"], ["1", "1"], [1, 2], {"p": "1", "n": "2"}):
            data = load_design(EXAMPLES / "voltage_divider.json").to_dict()
            data["root"]["components"][0]["parameters"]["spice"]["pins"] = mapping
            with tempfile.TemporaryDirectory() as directory, self.subTest(mapping=mapping):
                with self.assertRaises(SpiceError):
                    generate_spice(data, {"kind": "op"}, Path(directory) / "out.cir")
        data["root"]["components"][0]["pins"]["3"] = "EXTRA"
        data["root"]["components"][0]["parameters"]["spice"]["pins"] = ["1", "2"]
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(SpiceError):
            generate_spice(data, {"kind": "op"}, Path(directory) / "out.cir")

    def test_spice_value_never_accepts_cards_or_trailing_units(self):
        for value in ("1k\n.control\nshell touch bad", "1k; .include secret", "1kOhm", "{secret}", "$(anything)", "nan", "inf", "1e999", "1e-999"):
            data = load_design(EXAMPLES / "voltage_divider.json").to_dict()
            data["root"]["children"][0]["components"][0]["parameters"]["spice"]["value"] = value
            with tempfile.TemporaryDirectory() as directory, self.subTest(value=value):
                with self.assertRaises(SpiceError):
                    generate_spice(data, {"kind": "op"}, Path(directory) / "out.cir")

    def test_strict_si_values_are_emitted_as_unambiguous_numbers(self):
        data = load_design(EXAMPLES / "voltage_divider.json").to_dict()
        for literal, expected in (("1m", 0.001), ("1M", 1e6), ("1Meg", 1e6), ("4.7k", 4700), ("1e3", 1000)):
            data["root"]["children"][0]["components"][0]["parameters"]["spice"]["value"] = literal
            with tempfile.TemporaryDirectory() as directory, self.subTest(literal=literal):
                result = generate_spice(data, {"kind": "op"}, Path(directory) / "out.cir")
                self.assertEqual(next(model for model in result["models"] if model["reference"] == "R1")["value_si"], expected)

    def test_names_cannot_inject_netlist_syntax(self):
        data = load_design(EXAMPLES / "voltage_divider.json").to_dict()
        data["name"] = "arbitrary\n.include hidden"
        source = data["root"]["components"][0]
        source["reference"] = "J1\n.control"
        for comp in [source, *data["root"]["children"][0]["components"]]:
            comp["pins"] = {pin: "VIN\n.shell" if net == "VIN" else net for pin, net in comp["pins"].items()}
        with tempfile.TemporaryDirectory() as directory:
            result = generate_spice(data, {"kind": "op"}, Path(directory) / "out.cir")
            text = Path(result["path"]).read_text()
            self.assertNotIn(".control", text)
            self.assertNotIn(".include", text)
            self.assertNotIn(".shell", text)
            _validate_netlist(text)

    def test_ground_aliases_are_explicit_and_case_sensitive(self):
        data = load_design(EXAMPLES / "voltage_divider.json").to_dict()
        for comp in [*data["root"]["components"], *data["root"]["children"][0]["components"]]:
            comp["pins"] = {pin: "AGND" if net == "GND" else net for pin, net in comp["pins"].items()}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(SpiceError):
                generate_spice(data, {"kind": "op"}, root / "bad.cir")
            result = generate_spice(data, {"kind": "op"}, root / "ok.cir", ground_nets=["AGND"])
            self.assertEqual(result["net_map"]["AGND"], "0")

    def test_block_selection_does_not_invent_missing_sources(self):
        design = load_design(EXAMPLES / "voltage_divider.json")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = generate_spice(design, {"kind": "op"}, root / "block.cir", block_path="board.divider")
            self.assertEqual(set(result["reference_map"]), {"R1", "R2"})
            with self.assertRaises(SpiceError):
                generate_spice(design, {"kind": "dc", "source": "J1", "start": 0, "stop": 5, "step": 1}, root / "no_source.cir", block_path="board.divider")

    def test_invalid_analysis_values_and_tolerances_are_rejected(self):
        design = load_design(EXAMPLES / "rc_filter.json")
        bad_analyses = [{"kind": "op", "cards": ".include bad"}, {"kind": "tran", "step_s": True, "stop_s": 1},
                        {"kind": "tran", "step_s": 0.1, "stop_s": float("inf")}, {"kind": "tran", "step_s": 1, "stop_s": 0.1},
                        {"kind": "ac", "scale": "dec", "points": 1.5, "start_hz": 1, "stop_hz": 100},
                        {"kind": "dc", "source": "J1", "start": 0, "stop": 5, "step": -1},
                        {"kind": "dc", "source": "R1", "start": 0, "stop": 5, "step": 1}]
        for analysis in bad_analyses:
            with tempfile.TemporaryDirectory() as directory, self.subTest(analysis=analysis), self.assertRaises(SpiceError):
                generate_spice(design, analysis, Path(directory) / "out.cir")
        for tolerance in (-1, 2, "1%", True, float("nan")):
            data = design.to_dict()
            data["root"]["components"][1]["parameters"]["spice"]["tolerance"] = tolerance
            with tempfile.TemporaryDirectory() as directory, self.subTest(tolerance=tolerance), self.assertRaises(SpiceError):
                generate_spice(data, {"kind": "op"}, Path(directory) / "out.cir")

    def test_dnp_parts_are_excluded_and_existing_output_is_preserved(self):
        data = load_design(EXAMPLES / "voltage_divider.json").to_dict()
        dnp = copy.deepcopy(data["root"]["components"][0])
        dnp.update(id="unfitted", reference="U99", fitted=False, parameters={})
        data["root"]["components"].append(dnp)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "out.cir"
            result = generate_spice(data, {"kind": "op"}, output)
            self.assertEqual(result["excluded_dnp"], ["board.unfitted"])
            original = output.read_bytes()
            with self.assertRaises(SpiceError):
                generate_spice(data, {"kind": "op"}, output)
            self.assertEqual(output.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
