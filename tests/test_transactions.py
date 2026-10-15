import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pcb_dev.model import Block, Board, Design, Port, freeze_fingerprint
from pcb_dev.transactions import (
    DesignSession, TransactionError, apply_patch, atomic_write_json, read_json, verify_audit,
)


def project():
    return Design("transaction", "0.1.0", Board(30, 20), Block(
        "board", parameters={"voltage": 3.3}, children=[
            Block("power", ports=[Port("out", "power", "output", 3.2, 3.4, 100)]),
            Block("load", ports=[Port("in", "power", "input", 3, 3.6, current_draw_ma=50)], depends_on=["board.power"]),
        ]))


def connected_project():
    return apply_patch(project(), [{"op": "connect", "source": "board.power.out", "target": "board.load.in", "net": "VCC"}],
                       prevent_regressions=False)


def parameter(value, block="board", name="voltage"):
    return {"op": "set_parameter", "block": block, "name": name, "value": value}


class TransactionTests(unittest.TestCase):
    def test_success_returns_independent_design_and_preserves_original(self):
        original = connected_project()
        fingerprint = original.fingerprint()
        changed = apply_patch(original, [parameter(5)])
        self.assertEqual(changed.root.parameters["voltage"], 5)
        self.assertEqual(original.fingerprint(), fingerprint)
        changed.root.parameters["nested"] = [1]
        self.assertNotIn("nested", original.root.parameters)

    def test_batch_is_atomic_on_late_failure(self):
        original = connected_project()
        fingerprint = original.fingerprint()
        with self.assertRaises(TransactionError):
            apply_patch(original, [parameter(5), parameter(2, "board.missing")])
        self.assertEqual(original.fingerprint(), fingerprint)

    def test_add_block_and_connect_in_single_batch(self):
        original = connected_project()
        candidate = apply_patch(original, [
            {"op": "add_block", "parent": "board", "block": {
                "id": "extra", "ports": [{"id": "in", "kind": "power", "direction": "input",
                                          "voltage_min": 3, "voltage_max": 3.6, "current_draw_ma": 10}]}},
            {"op": "connect", "source": "board.power.out", "target": "board.extra.in", "net": "VCC"},
        ])
        self.assertEqual(len(candidate.connections), 2)
        self.assertEqual(candidate.root.children[-1].id, "extra")
        self.assertEqual(len(original.root.children), 2)

    def test_remove_connection_uses_matching_unordered_endpoints(self):
        candidate = apply_patch(connected_project(), [{"op": "remove_connection", "source": "board.load.in",
                                                      "target": "board.power.out", "net": "VCC"}],
                                prevent_regressions=False)
        self.assertEqual(candidate.connections, [])
        with self.assertRaisesRegex(TransactionError, "matching connection"):
            apply_patch(candidate, [{"op": "remove_connection", "source": "board.load.in",
                                     "target": "board.power.out", "net": "VCC"}])

    def test_disconnection_new_constraint_and_budget_regressions_reject(self):
        candidate = connected_project()
        proposals = [
            [{"op": "remove_connection", "source": "board.load.in", "target": "board.power.out", "net": "VCC"}],
            [{"op": "update_block", "block": "board", "changes": {
                "constraints": [{"expression": "voltage > 10", "message": "undervoltage"}]}}],
            [{"op": "connect", "source": "board.power.out", "target": "board.power.out", "net": "BAD"}],
        ]
        for proposal in proposals:
            with self.subTest(proposal=proposal), self.assertRaisesRegex(TransactionError, "regression"):
                apply_patch(candidate, proposal)

    def test_already_failing_budget_can_improve_but_cannot_worsen(self):
        candidate = connected_project()
        candidate.root.children[1].ports[0].current_draw_ma = 110
        good = {"op": "add_block", "parent": "board", "block": {"id": "empty"}}
        self.assertEqual(apply_patch(candidate, [good]).root.children[-1].id, "empty")
        # Ports are intentionally outside the general update allowlist. Exercise
        # the guard through an added load on the same already-overloaded rail.
        with self.assertRaisesRegex(TransactionError, "power.budget_exceeded"):
            apply_patch(candidate, [
                {"op": "add_block", "parent": "board", "block": {"id": "worse", "ports": [
                    {"id": "in", "kind": "power", "direction": "input", "voltage_min": 3,
                     "voltage_max": 3.6, "current_draw_ma": 1000}]}},
                {"op": "connect", "source": "board.power.out", "target": "board.worse.in", "net": "VCC"},
            ])

    def test_frozen_direct_descendant_and_context_edits_are_rejected(self):
        original = connected_project()
        protected = original.root.children[1]
        protected.frozen = True
        protected.frozen_hash = freeze_fingerprint(original, "board.load")
        for operation in (parameter(4, "board.load"), parameter(5),
                          {"op": "update_block", "block": "board.power", "changes": {"description": "upstream edit"}},
                          {"op": "connect", "source": "board.power.out", "target": "board.load.in", "net": "ALT"}):
            with self.subTest(operation=operation), self.assertRaisesRegex(TransactionError, "frozen"):
                apply_patch(original, [operation], prevent_regressions=False)
        unchanged = apply_patch(original, [{"op": "add_block", "parent": "board", "block": {"id": "unrelated"}}])
        self.assertEqual(unchanged.root.children[-1].id, "unrelated")

    def test_stale_or_missing_frozen_record_prevents_transactions(self):
        original = connected_project()
        original.root.children[0].frozen = True
        with self.assertRaisesRegex(TransactionError, "valid recorded fingerprint"):
            apply_patch(original, [parameter(5)])

    def test_shared_rail_branch_cannot_change_a_frozen_blocks_electrical_context(self):
        original = connected_project()
        original = apply_patch(original, [
            {"op": "add_block", "parent": "board", "block": {"id": "relay", "ports": [
                {"id": "rail", "kind": "power", "direction": "passive", "required": False}]}},
            {"op": "connect", "source": "board.power.out", "target": "board.relay.rail", "net": "VCC"},
        ])
        original.root.children[1].frozen = True
        original.root.children[1].frozen_hash = freeze_fingerprint(original, "board.load")
        with self.assertRaisesRegex(TransactionError, "invalidate a frozen"):
            apply_patch(original, [
                {"op": "add_block", "parent": "board", "block": {"id": "branch", "ports": [
                    {"id": "in", "kind": "power", "direction": "input", "voltage_min": 3,
                     "voltage_max": 3.6, "current_draw_ma": 1}]}},
                {"op": "connect", "source": "board.relay.rail", "target": "board.branch.in", "net": "VCC"},
            ])

    def test_forbidden_fields_code_paths_and_gate_changes(self):
        operations = [
            {"op": "set_parameter", "block": "board", "name": "a", "value": 1, "extra": 2},
            {"op": "set", "path": "/root/frozen", "value": False},
            {"op": "export", "path": "../release"},
            {"op": "update_block", "block": "board", "changes": {"frozen": False}},
            {"op": "update_block", "block": "board", "changes": {"id": "renamed"}},
            {"op": "add_block", "parent": "board", "block": {"id": "x", "frozen": True}},
            {"op": "add_block", "parent": "board", "block": {"id": "x", "children": [{"id": "y", "frozen_hash": "0" * 64}]}},
            parameter(float("nan")), parameter(1, "../board"),
        ]
        for operation in operations:
            with self.subTest(operation=operation), self.assertRaises(TransactionError):
                apply_patch(connected_project(), [operation])

    def test_schema_validation_cannot_be_bypassed_in_sketch_mode(self):
        original = connected_project()
        for operation in (
            {"op": "update_block", "block": "board", "changes": {"parameters": None}},
            {"op": "add_block", "parent": "board", "block": {"id": "x", "mystery": 1}},
            {"op": "connect", "source": "board.unknown.port", "target": "board.load.in", "net": "X"},
        ):
            with self.subTest(operation=operation), self.assertRaises(TransactionError):
                apply_patch(original, [operation], prevent_regressions=False)

    def test_invalid_arrays_and_limits(self):
        for operations in (None, {}, [parameter(1)] * 129, [None]):
            with self.subTest(operations=type(operations)), self.assertRaises(TransactionError):
                apply_patch(connected_project(), operations)

    def test_session_has_one_undo_step_for_whole_batch(self):
        original = connected_project()
        session = DesignSession(original, actor="engineer")
        session.apply([parameter(5), parameter(2, name="revision")])
        self.assertEqual(session.design.root.parameters["revision"], 2)
        self.assertEqual(session.undo().fingerprint(), original.fingerprint())
        self.assertEqual([event["action"] for event in session.history], ["apply", "undo"])
        with self.assertRaisesRegex(TransactionError, "no transaction"):
            session.undo()

    def test_failed_session_apply_has_no_audit_or_partial_edit(self):
        session = DesignSession(connected_project())
        before = session.design.fingerprint()
        with self.assertRaises(TransactionError):
            session.apply([parameter(5), parameter(2, "missing")])
        self.assertEqual(session.history, [])
        self.assertEqual(session.design.fingerprint(), before)

    def test_session_returns_cloned_design_and_audit(self):
        session = DesignSession(connected_project())
        output = session.apply([parameter(5)])
        output.root.parameters["voltage"] = 100
        history = session.history
        history[0]["actor"] = "tampered"
        self.assertEqual(session.design.root.parameters["voltage"], 5)
        self.assertEqual(session.history[0]["actor"], "human")

    def test_save_load_replays_audit_and_undo_stack(self):
        session = DesignSession(connected_project(), actor="engineer")
        session.apply([parameter(5)])
        session.apply([parameter(10)])
        session.undo(actor="reviewer")
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "session.json"
            session.save(path)
            restored = DesignSession.load(path)
            self.assertEqual(restored.audit_head, session.audit_head)
            self.assertEqual(restored.design.fingerprint(), session.design.fingerprint())
            self.assertEqual(restored.undo().root.parameters["voltage"], 3.3)

    def test_hash_chain_detects_modified_reordered_and_truncated_middle_events(self):
        session = DesignSession(connected_project())
        session.apply([parameter(5)])
        session.apply([parameter(6)])
        initial_hash = connected_project().fingerprint()
        self.assertEqual(verify_audit(session.history, initial_hash), session.audit_head)
        for events in (list(reversed(session.history)), session.history[1:]):
            with self.subTest(events=events), self.assertRaises(TransactionError):
                verify_audit(events, initial_hash)
        tampered = session.history
        tampered[0]["actor"] = "attacker"
        with self.assertRaisesRegex(TransactionError, "hash mismatch"):
            verify_audit(tampered, initial_hash)

    def test_load_rejects_snapshot_drift_even_with_intact_event_chain(self):
        session = DesignSession(connected_project())
        session.apply([parameter(5)])
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "session.json"
            session.save(path)
            data = json.loads(path.read_text())
            data["current_design"]["root"]["parameters"]["voltage"] = 99
            path.write_text(json.dumps(data))
            with self.assertRaisesRegex(TransactionError, "differs from the replayed audit"):
                DesignSession.load(path)

    def test_atomic_save_failure_preserves_existing_file_and_cleans_temp(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "session.json"
            path.write_text("existing")
            with patch("pcb_dev.transactions.os.replace", side_effect=OSError("simulated failure")):
                with self.assertRaises(TransactionError):
                    atomic_write_json({"new": True}, path)
            self.assertEqual(path.read_text(), "existing")
            self.assertEqual([p.name for p in Path(temp).iterdir()], ["session.json"])

    def test_read_json_rejects_duplicate_nonfinite_and_oversized_data(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "data.json"
            for text in ('{"a": 1, "a": 2}', '{"a": NaN}', '{"a": 1e999}', '{'):
                path.write_text(text)
                with self.subTest(text=text), self.assertRaises(TransactionError):
                    read_json(path)
            path.write_text('{"a": 1}')
            with self.assertRaises(TransactionError):
                read_json(path, max_bytes=4)


if __name__ == "__main__":
    unittest.main()
