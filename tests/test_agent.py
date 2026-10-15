import json
import unittest

from pcb_dev.agent import AgentError, AgentHarness, Budget, ModelReply, ScriptedProvider, ToolCall, TOOL_SCHEMAS, redact
from pcb_dev.model import Block, Board, Constraint, Design, freeze_fingerprint


def design():
    return Design("agent-test", "0.1.0", Board(40, 30), Block("board"))


def patch(operations, identifier="patch1", reason="Implement the requested design change"):
    return ModelReply(tool_calls=(ToolCall(identifier, "apply_design_patch", {"reason": reason, "operations": operations}),))


def result_events(result):
    return [event for event in result.trace if event["event"] == "tool_result"]


class AgentTests(unittest.TestCase):
    def test_deterministic_offline_edit_does_not_mutate_source(self):
        source = design()
        initial = source.fingerprint()
        replies = [ModelReply(tool_calls=(ToolCall("inspect1", "inspect_design", {}),)),
                   patch([{"op": "set_parameter", "block": "board", "name": "ambient", "value": 25}]),
                   ModelReply("Added ambient-temperature metadata. Native checks are still required.")]
        first = AgentHarness(ScriptedProvider(replies)).run(source, "Set ambient temperature to 25 degrees C")
        second = AgentHarness(ScriptedProvider(replies)).run(source, "Set ambient temperature to 25 degrees C")
        self.assertEqual(first.status, "model_finished")
        self.assertEqual(first.design.root.parameters["ambient"], 25)
        self.assertEqual(source.fingerprint(), initial)
        self.assertEqual(first.trace_json(), second.trace_json())
        self.assertFalse(first.fabrication_ready)
        self.assertFalse(first.to_dict()["fabrication_ready"])

    def test_create_and_connect_are_one_transaction(self):
        operations = [
            {"op": "add_block", "parent": "board", "block": {"id": "tx", "ports": [{"id": "data", "kind": "digital", "direction": "output"}]}},
            {"op": "add_block", "parent": "board", "block": {"id": "rx", "ports": [{"id": "data", "kind": "digital", "direction": "input"}]}},
            {"op": "connect", "source": "board.tx.data", "target": "board.rx.data", "net": "DATA"},
        ]
        result = AgentHarness(ScriptedProvider([patch(operations)])).run(design(), "Add a digital interface")
        self.assertEqual(len(result.design.root.children), 2)
        self.assertEqual(len(result.design.connections), 1)
        self.assertTrue(result_events(result)[0]["result"]["ok"])
        self.assertFalse(result.final_report.blocking)

    def test_invalid_atomic_batch_rolls_back_all_changes(self):
        source = design()
        operations = [
            {"op": "set_parameter", "block": "board", "name": "changed", "value": True},
            {"op": "add_block", "parent": "board", "block": {"id": "floating", "ports": [{"id": "vcc", "kind": "power", "direction": "input"}]}},
        ]
        result = AgentHarness(ScriptedProvider([patch(operations)])).run(source, "Try unsafe batch")
        self.assertEqual(result.design.fingerprint(), source.fingerprint())
        self.assertFalse(result_events(result)[0]["result"]["ok"])

    def test_frozen_blocks_cannot_be_modified(self):
        source = design()
        source.root.frozen_hash = freeze_fingerprint(source, "board")
        source.root.frozen = True
        result = AgentHarness(ScriptedProvider([patch([{"op": "set_parameter", "block": "board", "name": "ambient", "value": 25}])])).run(source, "Change frozen block")
        self.assertEqual(result.design.fingerprint(), source.fingerprint())
        self.assertFalse(result_events(result)[0]["result"]["ok"])

    def test_model_cannot_waive_export_shell_or_add_frozen_records(self):
        calls = [ToolCall(f"bad{i}", name, {}) for i, name in enumerate(("waive", "export_gerbers", "shell", "read_file", "fetch_url"))]
        replies = [ModelReply(tool_calls=tuple(calls)),
                   patch([{"op": "add_block", "parent": "board", "block": {"id": "fake", "frozen": True, "frozen_hash": "a" * 64}}]),
                   patch([{"op": "remove_connection", "source": "a", "target": "b", "net": "N"}], identifier="patch2")]
        source = design()
        result = AgentHarness(ScriptedProvider(replies)).run(source, "Ignore any request to bypass the gates")
        self.assertEqual(result.status, "no_progress")
        self.assertEqual(result.design.fingerprint(), source.fingerprint())
        self.assertTrue(all(not event["result"]["ok"] for event in result_events(result)))

    def test_strict_tool_args_reject_unknown_duplicate_nonfinite_fields(self):
        calls = [ToolCall("unknown", "check_design", {"waive": True}),
                 ToolCall("duplicate", "inspect_design", '{"block_path":"board","block_path":"other"}'),
                 ToolCall("nonfinite", "apply_design_patch", '{"reason":"bad","operations":[],"extra":NaN}')]
        result = AgentHarness(ScriptedProvider([ModelReply(tool_calls=tuple(calls))])).run(design(), "Inspect only")
        self.assertTrue(all(not event["result"]["ok"] for event in result_events(result)))
        self.assertIn("Duplicate", result_events(result)[1]["result"]["error"])

    def test_duplicate_tool_ids_do_not_apply_a_second_mutation(self):
        first = patch([{"op": "set_parameter", "block": "board", "name": "value", "value": 1}], identifier="same")
        second = patch([{"op": "set_parameter", "block": "board", "name": "value", "value": 2}], identifier="same")
        result = AgentHarness(ScriptedProvider([first, second])).run(design(), "Update one value")
        self.assertEqual(result.design.root.parameters["value"], 1)
        self.assertFalse(result_events(result)[1]["result"]["ok"])

    def test_repeated_noop_and_oscillating_designs_stop(self):
        repeats = [ModelReply(tool_calls=(ToolCall(f"check{i}", "check_design", {}),)) for i in range(8)]
        result = AgentHarness(ScriptedProvider(repeats), Budget(max_no_progress=2)).run(design(), "Review")
        self.assertEqual(result.status, "no_progress")
        self.assertEqual(result.steps, 2)
        toggles = [patch([{"op": "set_parameter", "block": "board", "name": "value", "value": i % 2}], identifier=f"toggle{i}") for i in range(8)]
        result = AgentHarness(ScriptedProvider(toggles), Budget(max_no_progress=2, max_tokens=500_000)).run(design(), "Avoid a loop")
        self.assertEqual(result.status, "no_progress")
        self.assertLessEqual(result.steps, 4)

    def test_step_and_token_budgets_bound_calls_before_execution(self):
        source = design()
        reply = patch([{"op": "set_parameter", "block": "board", "name": "value", "value": 1}])
        result = AgentHarness(ScriptedProvider([reply]), Budget(max_steps=1)).run(source, "Change one value")
        self.assertEqual(result.status, "step_budget")
        self.assertEqual(result.steps, 1)
        result = AgentHarness(ScriptedProvider([reply]), Budget(max_tokens=1)).run(source, "No budget")
        self.assertEqual(result.status, "token_budget")
        self.assertEqual(result.steps, 0)
        self.assertEqual(result.design.fingerprint(), source.fingerprint())

    def test_overbudget_provider_reply_is_not_executed(self):
        source = design()
        malicious = ModelReply(tool_calls=patch([{"op": "set_parameter", "block": "board", "name": "value", "value": 1}]).tool_calls,
                               output_tokens=1_000_000)
        result = AgentHarness(ScriptedProvider([malicious])).run(source, "Stay within budget")
        self.assertEqual(result.status, "token_budget")
        self.assertEqual(result.design.fingerprint(), source.fingerprint())
        self.assertFalse(result_events(result))

    def test_offline_mode_rejects_network_and_undeclared_providers(self):
        class NetworkProvider:
            network_enabled = True
            def complete(self, *args, **kwargs):
                raise AssertionError("must not call")
        with self.assertRaises(AgentError):
            AgentHarness(NetworkProvider())
        class UnknownProvider:
            def complete(self, *args, **kwargs):
                raise AssertionError("must not call")
        with self.assertRaises(AgentError):
            AgentHarness(UnknownProvider())

    def test_provider_cannot_mutate_authoritative_messages_or_tool_schema(self):
        class MutatingProvider:
            network_enabled = False
            def complete(self, messages, tools, **kwargs):
                messages[1]["content"] = "changed"
                tools[0].parameters["properties"]["shell"] = {"type": "string"}
                return ModelReply(tool_calls=(ToolCall("bad", "inspect_design", {"shell": "ignored"}),))
        result = AgentHarness(MutatingProvider(), Budget(max_steps=1)).run(design(), "Inspect")
        self.assertNotIn("shell", TOOL_SCHEMAS[0].parameters["properties"])
        self.assertFalse(result_events(result)[0]["result"]["ok"])

    def test_provider_errors_and_invalid_usage_fail_closed(self):
        class BrokenProvider:
            network_enabled = False
            def complete(self, *args, **kwargs):
                raise RuntimeError("Bearer SUPER_SECRET_CREDENTIAL")
        result = AgentHarness(BrokenProvider()).run(design(), "Inspect")
        self.assertEqual(result.status, "provider_error")
        self.assertNotIn("SUPER_SECRET_CREDENTIAL", result.trace_json())
        self.assertGreater(result.budget_tokens_used, 0)
        result = AgentHarness(ScriptedProvider([ModelReply(input_tokens=True)])).run(design(), "Inspect")
        self.assertEqual(result.status, "provider_error")

    def test_trace_and_public_result_redact_nested_secret_keys_and_json_args(self):
        source = design()
        source.root.parameters["api_key"] = "PRIVATE_VALUE"
        replies = [ModelReply(tool_calls=(ToolCall("inspect", "inspect_design", {}),)),
                   ModelReply("my secret is UNIQUE_SECRET")]
        result = AgentHarness(ScriptedProvider(replies), redaction_secrets=("UNIQUE_SECRET",)).run(source, "password=SECRET123")
        rendered = json.dumps(result.to_dict())
        for secret in ("PRIVATE_VALUE", "UNIQUE_SECRET", "SECRET123"):
            self.assertNotIn(secret, rendered)
        self.assertEqual(json.loads(redact('{"api_key":"VALUE"}'))["api_key"], "[REDACTED]")
        self.assertEqual(redact({"token": "VALUE"})["token"], "[REDACTED]")

    def test_finished_model_does_not_override_real_validation_failures(self):
        source = design()
        source.root.constraints = [Constraint("1 > 2", "Deliberately fails")]
        result = AgentHarness(ScriptedProvider([ModelReply("All checks passed; release approved.")])).run(source, "Review")
        self.assertEqual(result.status, "model_finished")
        self.assertTrue(result.final_report.blocking)
        self.assertFalse(result.fabrication_ready)

    def test_model_cannot_silently_delete_constraints_or_resource_declarations(self):
        source = design()
        source.root.constraints = [Constraint("1 > 2", "Deliberately fails")]
        source.root.resources = {"i2c": [{"bus": "I2C", "address": 0x48}]}
        for changes in ({"constraints": []}, {"constraints": [{"expression": "1 < 2", "message": "Changed requirement"}]}, {"resources": {}}):
            with self.subTest(changes=changes):
                result = AgentHarness(ScriptedProvider([patch([{"op": "update_block", "block": "board", "changes": changes}])])).run(source, "Review requirements")
                self.assertEqual(result.design.fingerprint(), source.fingerprint())
                self.assertFalse(result_events(result)[0]["result"]["ok"])

    def test_model_can_add_constraints_without_rewriting_existing_ones(self):
        source = design()
        source.root.constraints = [Constraint("1 < 2", "Existing requirement")]
        changes = {"constraints": [{"expression": "1 < 2", "message": "Existing requirement"}, {"expression": "2 < 3", "message": "Additional requirement"}]}
        result = AgentHarness(ScriptedProvider([patch([{"op": "update_block", "block": "board", "changes": changes}])])).run(source, "Add a requirement")
        self.assertEqual(len(result.design.root.constraints), 2)
        self.assertTrue(result_events(result)[0]["result"]["ok"])

    def test_budget_configuration_and_objective_are_strict(self):
        for arguments in ({"max_steps": 0}, {"max_tokens": True}, {"max_no_progress": -1}, {"max_tool_calls_per_step": 1000}):
            with self.subTest(arguments=arguments), self.assertRaises(AgentError):
                Budget(**arguments)
        with self.assertRaises(AgentError):
            AgentHarness(ScriptedProvider([])).run(design(), " ")


if __name__ == "__main__":
    unittest.main()
