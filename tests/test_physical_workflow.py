"""Public CLI and proposal boundaries for physical port intent."""

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest

from pcb_dev.agent import AgentError, AgentHarness, ModelReply, ScriptedProvider, ToolCall, _protect_requirements
from pcb_dev.cli import main
from pcb_dev.model import Design, load_design
from pcb_dev.pipeline import BuildError, build_preview


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "examples/library_divider"


class PhysicalWorkflowTests(unittest.TestCase):
    def invoke(self, *arguments):
        output, errors = StringIO(), StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = main(list(map(str, arguments)))
        return code, output.getvalue(), errors.getvalue()

    def test_strict_check_accepts_real_library_divider_interfaces(self):
        code, output, errors = self.invoke("check", FIXTURE / "port_design.json", "--require-physical-ports", "--json")
        self.assertEqual(code, 0, errors or output)
        coverage = json.loads(output)["metadata"]["physical_ports"]
        self.assertEqual(coverage["status"], "complete")
        self.assertEqual(coverage["realized_ports"], 4)

    def test_legacy_abstract_design_requires_explicit_strict_opt_in(self):
        source = ROOT / "examples/usb_mcu_led.json"
        self.assertEqual(self.invoke("check", source)[0], 0)
        code, output, _ = self.invoke("check", source, "--require-physical-ports", "--json")
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(output)["metadata"]["physical_ports"]["complete"])

    def test_strict_failure_publishes_no_partial_build(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "draft"
            with self.assertRaises(BuildError):
                build_preview(ROOT / "examples/usb_mcu_led.json", output, require_physical_ports=True)
            self.assertFalse(output.exists())

    def test_strict_library_build_records_policy_and_mapping(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "draft"
            code, text, errors = self.invoke("build", FIXTURE / "port_design.json", "--bindings",
                FIXTURE / "port_bindings.json", "--require-physical-ports", "--output", output)
            self.assertEqual(code, 0, errors or text)
            lock = json.loads((output / "build-lock.json").read_text(encoding="utf-8"))
            self.assertTrue(lock["physical_ports_required"])
            self.assertFalse(lock["fabrication_ready"])
            self.assertIn("board.divider.supply", (output / "design.md").read_text(encoding="utf-8"))
            self.assertEqual(self.invoke("verify", output)[0], 0)

    def test_agent_can_create_a_valid_declared_physical_interface(self):
        source = Design.from_dict({"schema_version": 1, "name": "agent-physical", "version": "0.3.0",
            "board": {"width_mm": 20, "height_mm": 20}, "root": {"id": "board"}})
        block = {"id": "interface", "ports": [{"id": "signal", "kind": "analog", "direction": "bidirectional",
            "required": False, "net": "SIGNAL", "pin_bindings": [{"component": "j1", "pin": "1"}]}],
            "components": [{"id": "j1", "reference": "J1", "value": "Test", "footprint": "unqualified",
                "pins": {"1": "SIGNAL"}}]}
        reply = ModelReply(tool_calls=(ToolCall("create", "apply_design_patch", {"reason": "Declare interface intent",
            "operations": [{"op": "add_block", "parent": "board", "block": block}]}),))
        result = AgentHarness(ScriptedProvider([reply])).run(source, "Add a declared interface")
        self.assertEqual(len(result.design.root.children), 1)
        self.assertEqual(result.design.root.children[0].ports[0].pin_bindings[0].pin, "1")
        self.assertFalse(result.fabrication_ready)
        self.assertEqual(source.root.children, [])

    def test_agent_guard_preserves_existing_net_and_pin_intent(self):
        before = load_design(FIXTURE / "port_design.json")
        changed = Design.from_dict(before.to_dict())
        changed.root.children[1].ports[0].net = "VBUS"
        changed.root.children[1].components[0].pins["1"] = "VBUS"
        with self.assertRaisesRegex(AgentError, "port-to-pin"):
            _protect_requirements(before, changed)


if __name__ == "__main__":
    unittest.main()
