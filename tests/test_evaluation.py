import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pcb_dev.agent import ModelReply, ToolCall
from pcb_dev.evaluation import CASES, run_evaluation
from pcb_dev.project import DemoManagerProvider


def fixture(divider):
    design = {"schema_version": 1, "name": "Explicit fixture", "version": "0.1.0", "board": {"width_mm": 40, "height_mm": 30, "layers": 2, "manufacturer": "jlcpcb"}, "root": {"id": "board"}, "connections": [], "metadata": {}}
    if divider:
        design["root"].update(parameters={"vin_v": 5, "r_top_ohm": 10000, "r_bottom_ohm": 10000}, constraints=[{"expression": "vin_v == 5 and r_top_ohm == 10000 and r_bottom_ohm == 10000", "message": "Supplied fixture conditions"}], components=[])
        for index, kind, value, nets in (("J1", "V", 5, ["VIN", "GND"]), ("R1", "R", 10000, ["VIN", "VOUT"]), ("R2", "R", 10000, ["VOUT", "GND"])):
            design["root"]["components"].append({"id": index.lower(), "reference": index, "value": str(value), "footprint": "Fixture:Generic", "pins": {"1": nets[0], "2": nets[1]}, "parameters": {"spice": {"kind": kind, "value": value, "pins": ["1", "2"]}}, "position": {"x_mm": 10, "y_mm": 10}})
    return design


class FixtureProvider:
    """Explicit scripted test, never a real model capability measurement."""
    network_enabled = False

    def __init__(self, *, bad=False, questions=False, claims_only=False):
        self.bad, self.questions, self.claims_only = bad, questions, claims_only

    def complete(self, messages, tools, *, max_tokens):
        del tools, max_tokens
        marker = "Current authoritative project snapshot: "
        state = json.loads(next(message["content"][len(marker):] for message in messages if message.get("content", "").startswith(marker)))
        if self.claims_only:
            return ModelReply("All tests passed; native validated and manufacturing ready.")
        if not state["brief"]:
            name, args = "propose_brief", {"title": "Explicit scripted fixture", "summary": "Test harness", "requirements": ["Supplied fixture conditions"], "assumptions": ["No real model is evaluated"], "suggestions": [], "questions": ["Invent an answer?"] if self.questions else []}
        elif not state["brief"]["approved"]:
            return ModelReply("Await test-host approval")
        elif not state["plan"]:
            name, args = "propose_plan", {"steps": [{"id": "create", "description": "Create fixture", "depends_on": [], "tool": "create_design", "acceptance": "Valid canonical data"}, {"id": "build", "description": "Build actual preview", "depends_on": ["create"], "tool": "build_draft", "acceptance": "Verified actual files"}]}
        elif state["design"] is None:
            divider = any("ideal unloaded" in message.get("content", "") for message in messages if message["role"] == "user")
            design = fixture(divider)
            if self.bad and divider:
                design["root"]["components"][2]["pins"]["1"] = "WRONG"
            name, args = "create_design", {"design": design}
        elif not any(message["role"] == "tool" and '"output"' in message["content"] for message in messages):
            name, args = "build_draft", {}
        else:
            return ModelReply("Actual draft files complete", input_tokens=100, output_tokens=10, usage_reported=True)
        return ModelReply("Scripted fixture " + name, (ToolCall(name, name, args),), input_tokens=100, output_tokens=10, usage_reported=True)


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name) / "evaluation"

    def test_both_cases_measure_actual_workflow_and_persist_reports(self):
        created = []
        def factory():
            provider = FixtureProvider()
            created.append(provider)
            return provider
        result = run_evaluation(self.output, factory)
        self.assertTrue(result["passed"], result)
        self.assertEqual(len(created), 2)
        self.assertEqual(result["summary"]["completed"], 2)
        self.assertGreater(result["summary"]["qwen_tokens"], 0)
        self.assertFalse(result["fabrication_ready"])
        self.assertFalse(result["model_quality_claimed"])
        self.assertTrue(Path(result["json_path"]).is_file())
        self.assertEqual(json.loads(Path(result["json_path"]).read_text(encoding="utf-8")), result)
        self.assertIn("No model qualification", Path(result["markdown_path"]).read_text(encoding="utf-8"))
        for case in result["cases"]:
            self.assertTrue(all(case["checks"].values()))
            self.assertEqual(case["approval"]["scope"], "disposable-test-fixture-only")
            self.assertGreater(case["calls"], 1)
            self.assertEqual(case["calls"], case["steps"])
            self.assertEqual(case["usage"]["claude_tokens"], 0)
            self.assertTrue((self.output / "cases" / case["id"] / "project" / "project.json").is_file())

    def test_questions_are_never_answered_or_approved_automatically(self):
        result = run_evaluation(self.output, lambda: FixtureProvider(questions=True), case_ids=["diagnostic-draft"])
        case = result["cases"][0]
        self.assertFalse(result["passed"])
        self.assertTrue(case["needs_input"])
        self.assertIsNone(case["approval"])
        self.assertFalse(case["checks"]["canonical_design"])
        self.assertEqual(case["status"], "awaiting_input")

    def test_narrative_success_cannot_replace_design_preflight_or_files(self):
        result = run_evaluation(self.output, lambda: FixtureProvider(claims_only=True), case_ids=["diagnostic-draft"])
        self.assertFalse(result["passed"])
        case = result["cases"][0]
        self.assertFalse(case["checks"]["actual_build_verified"])
        self.assertFalse(case["checks"]["preflight_passed"])
        self.assertFalse(case["fabrication_ready"])

    def test_wrong_electrical_topology_fails_despite_healthy_metadata_and_build(self):
        result = run_evaluation(self.output, lambda: FixtureProvider(bad=True), case_ids=["ideal-divider"])
        case = result["cases"][0]
        self.assertTrue(case["checks"]["preflight_passed"])
        self.assertTrue(case["checks"]["actual_build_verified"])
        self.assertFalse(case["checks"]["case_conditions"])
        self.assertFalse(result["passed"])

    def test_explicit_demo_proves_harness_only_and_gets_no_model_tokens(self):
        result = run_evaluation(self.output, DemoManagerProvider, case_ids=["diagnostic-draft"])
        self.assertTrue(result["passed"], result)
        self.assertTrue(result["cases"][0]["demo"])
        self.assertEqual(result["summary"]["qwen_tokens"], 0)
        self.assertFalse(result["model_quality_claimed"])

    def test_existing_output_invalid_case_and_duplicate_case_never_invoke_factory(self):
        with patch("pcb_dev.evaluation.ProjectManager") as manager:
            for case_ids in (["../escape"], ["diagnostic-draft"] * 2, []):
                with self.assertRaises(ValueError):
                    run_evaluation(self.output, FixtureProvider, case_ids=case_ids)
            self.output.mkdir()
            with self.assertRaises(ValueError):
                run_evaluation(self.output, FixtureProvider)
            manager.assert_not_called()
        self.assertEqual(set(CASES), {"diagnostic-draft", "ideal-divider"})

    def test_factory_failure_is_reported_without_secret_exception_text(self):
        def factory():
            raise RuntimeError("private-key-do-not-write")
        result = run_evaluation(self.output, factory, case_ids=["diagnostic-draft"])
        self.assertFalse(result["passed"])
        self.assertEqual(result["cases"][0]["calls"], 0)
        self.assertNotIn("private-key-do-not-write", Path(result["json_path"]).read_text(encoding="utf-8"))

    def test_changed_actual_artifact_fails_manifest_validation(self):
        from pcb_dev.evaluation import _build_matches
        def altered(manager, design):
            for artifact in manager.snapshot()["artifacts"]:
                if artifact["relative_path"].endswith("/design.json"):
                    manager.artifact_path(artifact["relative_path"]).write_text("{}", encoding="utf-8")
                    break
            return _build_matches(manager, design)
        with patch("pcb_dev.evaluation._build_matches", side_effect=altered):
            result = run_evaluation(self.output, FixtureProvider, case_ids=["diagnostic-draft"])
        self.assertFalse(result["passed"])
        self.assertIn("error", result["cases"][0])
