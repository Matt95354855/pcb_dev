import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pcb_dev.agent import AgentHarness, ModelReply, ToolCall
from pcb_dev.model import Design, freeze_fingerprint
from pcb_dev.project_store import ProjectError, ProjectStore, digest
from pcb_dev.project_tools import ProjectTools, design_contract
from pcb_dev.transactions import TransactionError, apply_patch


class ToolsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = ProjectStore(Path(self.temp.name) / "project")
        self.tools = ProjectTools(self.store)
        brief = {"title": "Test", "summary": "Prototype", "requirements": ["3.3V"], "assumptions": [], "suggestions": [], "questions": []}
        brief["hash"] = digest(brief)
        brief["approved"] = True
        self.state = {"schema_version": 1, "revision": 0, "brief": brief,
                      "approved_brief_hash": brief["hash"], "design": None, "preflight": None,
                      "artifacts": [], "status": "working", "fabrication_ready": False}

    def execute(self, name, arguments=None):
        return self.tools.execute(self.state, ToolCall("test", name, arguments or {}))[0]

    def create(self, value=None):
        self.execute("create_design", {"design": value or design_contract()["example"]})
        return Design.from_dict(self.state["design"])

    def test_contract_example_is_complete_valid_and_has_realized_ports(self):
        reference = self.execute("read_design_contract")
        self.assertIn("Component", reference["types"])
        design = Design.from_dict(reference["example"])
        self.create(design.to_dict())
        self.assertFalse(self.execute("check_design")["blocking"])
        self.assertEqual(self.execute("inspect_design", {"block_path": "board.load"})["block"]["components"][0]["reference"], "R1")

    def test_plan_requires_topological_order_unique_steps_and_known_tools(self):
        step = {"id": "a", "description": "x", "depends_on": [], "tool": "build_draft", "acceptance": "Files exist"}
        self.execute("propose_plan", {"steps": [step]})
        for steps in ([step, step], [{**step, "depends_on": ["missing"]}], [{**step, "tool": "shell"}]):
            with self.subTest(steps=steps), self.assertRaises(ProjectError):
                self.execute("propose_plan", {"steps": steps})

    def test_component_edit_value_pins_placement_is_atomic_and_keeps_identity(self):
        original = self.create()
        before = original.fingerprint()
        changes = {"value": "2k", "position": {"x_mm": 16, "y_mm": 11, "rotation": 90}, "pins": {"1": "VCC", "2": "RETURN"}}
        result = self.execute("apply_design_patch", {"reason": "Correct model", "operations": [{"op": "update_component", "component": "board.load.r1", "changes": changes}]})
        self.assertTrue(result["applied"])
        component = Design.from_dict(self.state["design"]).root.children[1].components[0]
        self.assertEqual(component.value, "2k")
        self.assertEqual(component.position.rotation, 90)
        self.assertEqual(component.pins["2"], "RETURN")
        self.assertEqual(original.fingerprint(), before)
        for changes in ({"id": "other"}, {"fitted": False}, {"reference": "R9"}, {"footprint": "other"}, {"pins": {"1": "VCC"}}, {}):
            with self.subTest(changes=changes), self.assertRaises(TransactionError):
                apply_patch(original, [{"op": "update_component", "component": "board.load.r1", "changes": changes}])

    def test_component_changes_keep_frozen_and_port_intent_protected(self):
        original = self.create()
        original.root.children[1].frozen = True
        original.root.children[1].frozen_hash = freeze_fingerprint(original, "board.load")
        with self.assertRaisesRegex(TransactionError, "frozen"):
            apply_patch(original, [{"op": "update_component", "component": "board.load.r1", "changes": {"value": "2k"}}])
        before = copy.deepcopy(self.state["design"])
        with self.assertRaisesRegex(TransactionError, "regression"):
            self.execute("apply_design_patch", {"reason": "Rewire", "operations": [{"op": "update_component", "component": "board.load.r1", "changes": {"pins": {"1": "OTHER", "2": "GND"}}}]})
        self.assertEqual(self.state["design"], before)

    def test_model_cannot_delete_constraints_resources_or_rating_evidence(self):
        value = design_contract()["example"]
        value["root"]["parameters"] = {"voltage": 3.3}
        value["root"]["constraints"] = [{"expression": "voltage <= 3.6", "message": "Keep voltage"}]
        value["root"]["resources"] = {"required_pins": [1]}
        component = value["root"]["children"][1]["components"][0]
        component["parameters"] = {"voltage_rating_v": 2, "applied_voltage_v": 3.3}
        self.create(value)
        proposals = [
            {"op": "update_block", "block": "board", "changes": {"constraints": []}},
            {"op": "update_block", "block": "board", "changes": {"resources": {}}},
            {"op": "update_component", "component": "board.load.r1", "changes": {"parameters": {}}},
            {"op": "update_component", "component": "board.load.r1", "changes": {"parameters": {"voltage_rating_v": 2, "applied_voltage_v": 1}}},
        ]
        before = copy.deepcopy(self.state["design"])
        for operation in proposals:
            with self.subTest(operation=operation), self.assertRaises(ValueError):
                self.execute("apply_design_patch", {"reason": "Try to hide evidence", "operations": [operation]})
            self.assertEqual(self.state["design"], before)

    def test_native_missing_tool_remains_blocked_and_draft_artifacts_are_confined(self):
        self.create()
        built = self.execute("build_draft")
        self.assertFalse(built["fabrication_ready"])
        self.assertTrue(self.state["artifacts"])
        unavailable = {stage: {"status": "unavailable", "executed": False, "reason": "KiCad missing"} for stage in ("erc", "drc")}
        with patch("pcb_dev.backends.kicad.KiCadBackend.check", return_value=unavailable), patch("pcb_dev.backends.kicad.KiCadBackend.export_netlist", return_value={"status": "unavailable", "reason": "KiCad missing"}):
            result = self.execute("native_check")
        self.assertEqual(result["status"], "blocked")
        self.assertFalse(result["validation_passed"])
        self.assertFalse(result["fabrication_ready"])
        self.assertTrue(all(not Path(item["relative_path"]).is_absolute() for item in self.state["artifacts"]))
        for item in self.state["artifacts"]:
            self.assertTrue(self.store.safe_path(item["relative_path"]).is_file())

    def test_edit_invalidates_native_evidence_and_requires_a_new_build(self):
        self.create()
        self.execute("build_draft")
        self.state["native_evidence"] = {"design_sha256": Design.from_dict(self.state["design"]).fingerprint(), "status": "passed"}
        self.execute("apply_design_patch", {"reason": "Update", "operations": [{"op": "update_component", "component": "board.load.r1", "changes": {"value": "2k"}}]})
        self.assertTrue(self.state["native_evidence"]["stale"])
        with self.assertRaisesRegex(ProjectError, "stale"):
            self.execute("native_check")

    def test_simulation_uses_explicit_generator_and_retains_missing_engine_evidence(self):
        path = Path(__file__).resolve().parents[1] / "examples" / "voltage_divider.json"
        self.create(json.loads(path.read_text(encoding="utf-8")))
        with patch("pcb_dev.backends.NgSpiceBackend.simulate", return_value={"status": "unavailable", "executed": False, "hardware_validated": False, "reason": "Ngspice missing"}) as simulate:
            result = self.execute("simulate", {"analysis": {"kind": "op"}})
        self.assertEqual(result["status"], "unavailable")
        self.assertTrue(simulate.call_args.args[0].is_file())
        self.assertFalse(result["fabrication_ready"])
        self.assertTrue(any(item["relative_path"].endswith("circuit.cir") for item in self.state["artifacts"]))

    def test_path_library_and_executable_arguments_are_forbidden(self):
        self.create()
        for name, args in (("build_draft", {"output": "../escape"}), ("native_check", {"executable": "/bin/sh"}), ("simulate", {"analysis": {"kind": "op"}, "output": "elsewhere"})):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.execute(name, args)
        self.assertFalse((Path(self.temp.name) / "escape").exists())

    def test_legacy_harness_does_not_gain_component_edit_authority(self):
        class Provider:
            network_enabled = False
            def complete(self, *args, **kwargs):
                return ModelReply("done")
        original = self.create()
        call = ToolCall("edit", "apply_design_patch", {"reason": "Edit", "operations": [{"op": "update_component", "component": "board.load.r1", "changes": {"value": "2k"}}]})
        with self.assertRaisesRegex(ValueError, "forbidden"):
            AgentHarness(Provider())._tool(original, call)
