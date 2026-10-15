"""Review coverage for newer command boundaries, using no network or EDA calls."""

import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pcb_dev.cli import main


EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
DESIGN = EXAMPLES / "usb_mcu_led.json"


class CLIReviewTests(unittest.TestCase):
    def run_cli(self, *args):
        output, error = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
            status = main([str(arg) for arg in args])
        return status, output.getvalue(), error.getvalue()

    def test_malformed_script_tool_entries_are_input_errors_without_traceback(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            for index, replies in enumerate(([{"tool_calls": [None]}], [{"tool_calls": [{"name": "check_design"}]}], [{"unexpected": True}])):
                with self.subTest(replies=replies):
                    script = base / f"script-{index}.json"
                    script.write_text(json.dumps({"replies": replies}), encoding="utf-8")
                    output = base / f"output-{index}"
                    status, _, error = self.run_cli("agent-demo", DESIGN, "--script", script, "--output", output)
                    self.assertEqual(status, 2)
                    self.assertIn("pcb-dev:", error)
                    self.assertNotIn("Traceback", error)
                    self.assertFalse(output.exists())

    def test_agent_budget_invalid_input_and_exhaustion_are_distinct(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            status, _, error = self.run_cli("agent-demo", DESIGN, "--script", EXAMPLES / "agent_session.json",
                                            "--output", base / "invalid", "--max-steps", 0)
            self.assertEqual(status, 2, error)
            self.assertFalse((base / "invalid").exists())
            status, _, error = self.run_cli("agent-demo", DESIGN, "--script", EXAMPLES / "agent_session.json",
                                            "--output", base / "exhausted", "--max-tokens", 1)
            self.assertEqual(status, 1, error)
            session = json.loads((base / "exhausted" / "session.json").read_text(encoding="utf-8"))
            self.assertEqual(session["status"], "token_budget")
            self.assertFalse(session["fabrication_ready"])

    def test_remote_permissions_fail_before_any_model_request(self):
        cases = [({"offline": True, "allow_network": False}, []),
                 ({"offline": False, "allow_network": True}, []),
                 ({"offline": True, "allow_network": False}, ["--allow-network"])]
        with tempfile.TemporaryDirectory() as directory, patch("pcb_dev.providers.ChatCompletionsProvider.complete", side_effect=AssertionError("No network request is authorized")) as complete:
            base = Path(directory)
            for index, (permissions, extra) in enumerate(cases):
                with self.subTest(permissions=permissions, extra=extra):
                    config = base / f"config-{index}.json"
                    config.write_text(json.dumps({"profile": {"model": "explicit-model"}, "endpoint": "https://remote.invalid/v1", **permissions}), encoding="utf-8")
                    output = base / f"output-{index}"
                    status, _, error = self.run_cli("agent-run", DESIGN, "--config", config, "--objective", "Review only", "--output", output, *extra)
                    self.assertEqual(status, 2, error)
                    self.assertFalse(output.exists())
            complete.assert_not_called()

    def test_bad_provider_configuration_is_an_input_error(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            for index, data in enumerate(({"profile": None}, {"profile": {"model": "test"}, "api_key": "must-not-be-literal"})):
                config = base / f"bad-{index}.json"
                config.write_text(json.dumps(data), encoding="utf-8")
                status, _, error = self.run_cli("agent-run", DESIGN, "--config", config, "--objective", "Review", "--output", base / f"out-{index}")
                self.assertEqual(status, 2, error)
                self.assertNotIn("must-not-be-literal", error)

    def test_package_create_pin_inspect_and_tamper_detection(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            package, lock = base / "demo.json", base / "blocks.lock.json"
            status, text, error = self.run_cli("package", "create", DESIGN, "board", "--name", "usb-demo", "--output", package)
            self.assertEqual(status, 0, error)
            self.assertEqual(json.loads(text)["name"], "usb-demo")
            status, text, error = self.run_cli("package", "pin", package, "--lock", lock)
            self.assertEqual(status, 0, error)
            self.assertEqual(json.loads(text)["file"], "demo.json")
            status, text, error = self.run_cli("package", "inspect", "usb-demo", "--lock", lock)
            self.assertEqual(status, 0, error)
            self.assertEqual(json.loads(text)["root"]["id"], "board")
            package.write_bytes(package.read_bytes() + b"\n")
            status, _, error = self.run_cli("package", "inspect", "usb-demo", "--lock", lock)
            self.assertEqual(status, 2)
            self.assertIn("SHA-256", error)

    def test_package_external_dependency_and_existing_output_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "ldo.json"
            status, _, error = self.run_cli("package", "create", DESIGN, "board.power", "--name", "power-ldo", "--output", output)
            self.assertEqual(status, 2)
            self.assertIn("external dependency", error)
            self.assertFalse(output.exists())
            output.write_text("keep", encoding="utf-8")
            status, _, error = self.run_cli("package", "create", DESIGN, "board", "--name", "usb-demo", "--output", output)
            self.assertEqual(status, 2)
            self.assertEqual(output.read_text(encoding="utf-8"), "keep")

    def test_native_check_absent_binary_reports_blocked_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            board, schematic, output = base / "review.kicad_pcb", base / "review.kicad_sch", base / "native"
            board.write_text("(kicad_pcb)", encoding="utf-8")
            schematic.write_text("(kicad_sch)", encoding="utf-8")
            status, text, error = self.run_cli("native-check", DESIGN, "--board", board, "--schematic", schematic,
                                              "--output", output, "--kicad-cli", base / "missing" / "kicad-cli", "--json")
            self.assertEqual(status, 1, error)
            evidence = json.loads(text)
            self.assertFalse(evidence["validation_passed"])
            self.assertFalse(evidence["fabrication_ready"])
            self.assertTrue(evidence["gate"]["blocking"])
            native = [report for report in evidence["reports"] if report["stage"] in ("erc", "drc")]
            self.assertEqual(len(native), 2)
            self.assertTrue(all(not report["metadata"]["completed"] for report in native))
            self.assertTrue((output / "checks.json").is_file())
            self.assertTrue((output / "checks.md").is_file())

    def test_native_invalid_timeout_is_caught_as_input_error(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            status, _, error = self.run_cli("native-check", DESIGN, "--board", base / "board.kicad_pcb", "--schematic", base / "schematic.kicad_sch",
                                            "--output", base / "out", "--timeout", "nan", "--kicad-cli", base / "missing" / "kicad-cli")
            self.assertEqual(status, 2)
            self.assertNotIn("Traceback", error)

    def test_tutorial_tolerance_arguments_and_duplicate_rejection(self):
        status, text, error = self.run_cli("tolerance", DESIGN, "board.led", "--relative", "led_forward_v=0.05", "--relative", "led_current_a=0.01", "--samples", 100, "--seed", 42)
        self.assertEqual(status, 0, error)
        self.assertEqual(json.loads(text)["failed_samples"], 0)
        status, _, error = self.run_cli("tolerance", DESIGN, "board.led", "--relative", "led_forward_v=0.05", "--relative", "led_forward_v=0.01")
        self.assertEqual(status, 2)
        self.assertIn("unique", error)


if __name__ == "__main__":
    unittest.main()
