"""Human decisions, immutable versions and stale-context adversarial scenarios."""

from copy import deepcopy
import json
from pathlib import Path
from unittest.mock import patch
import tempfile
import unittest

from pcb_dev.agent import ModelReply, ToolCall
from pcb_dev.model import Design
from pcb_dev.project import ProjectError, ProjectManager
from pcb_dev.project_store import clone
from pcb_dev.versions import record_version, semantic_diff
from test_project import Provider, brief, call, design


def operation(value=5):
    return {"op": "set_parameter", "block": "board", "name": "voltage", "value": value}


class ProjectReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "project"

    def ready(self, *, review=True, value=None):
        provider = Provider([call("brief", "propose_brief", brief()), ModelReply("Approve"),
                             call("create", "create_design", {"design": value or design()}), ModelReply("Draft")])
        manager = ProjectManager(self.directory, provider, review_patches=review)
        manager.send_message("A prototype")
        manager.approve_brief()
        return manager, provider

    def propose(self, manager, provider, ops=None, trailing=()):
        provider.replies = [ModelReply(tool_calls=(ToolCall("patch", "apply_design_patch", {
            "reason": "Corriger la tension", "operations": [operation()] if ops is None else ops}), *trailing)), ModelReply("must not run")]
        state = manager.send_message("Propose une correction")
        proposal = state["patch_proposals"][-1] if state["patch_proposals"] else None
        return state, proposal

    def test_optional_review_keeps_legacy_autonomous_mode_and_versions_every_mutation(self):
        manager, provider = self.ready(review=False)
        previous = manager.snapshot()["design_versions"][0]["design_hash"]
        state, _ = self.propose(manager, provider)
        self.assertFalse(state["review_patches"])
        self.assertEqual(state["patch_proposals"], [])
        self.assertEqual(state["design"]["root"]["parameters"]["voltage"], 5)
        self.assertEqual(len(state["design_versions"]), 2)
        comparison = manager.compare_versions(previous, state["design_versions"][-1]["design_hash"])
        self.assertTrue(comparison["read_only"])
        self.assertEqual(comparison["changes"][0]["path"], "/root/parameters/voltage")

    def test_review_preserves_current_design_and_stops_the_remaining_model_batch(self):
        manager, provider = self.ready()
        before = manager.snapshot()
        calls = len(provider.calls)
        state, proposal = self.propose(manager, provider, trailing=(ToolCall("build", "build_draft", {}),))
        self.assertEqual(state["status"], "patch_review")
        self.assertEqual(state["design"], before["design"])
        self.assertEqual(state["preflight"], before["preflight"])
        self.assertEqual(state["design_versions"], before["design_versions"])
        self.assertEqual(len(provider.calls), calls + 1)
        self.assertEqual(proposal["status"], "pending")
        self.assertNotEqual(proposal["candidate_design_hash"], proposal["base_design_hash"])
        results = [json.loads(item["content"]) for item in manager._state["messages"] if item["role"] == "tool"]
        self.assertTrue(results[-1]["deferred"])
        self.assertFalse(any(item["relative_path"].endswith("build-lock.json") for item in state["artifacts"]))
        calls = len(provider.calls)
        waiting = manager.send_message("Continue sans approbation")
        self.assertEqual(waiting["status"], "patch_review")
        self.assertEqual(len(provider.calls), calls)

    def test_accept_applies_once_audits_exact_hash_and_never_calls_a_model(self):
        manager, provider = self.ready()
        _, proposal = self.propose(manager, provider)
        calls, usage = len(provider.calls), manager.snapshot()["usage"]
        revision = manager.snapshot()["revision"]
        state = manager.accept_patch(proposal["proposal_hash"])
        self.assertEqual(state["revision"], revision + 1)
        self.assertEqual(state["design"]["root"]["parameters"]["voltage"], 5)
        self.assertEqual(state["patch_proposals"][-1]["decision"]["actor"], "human")
        self.assertEqual(state["design_versions"][-1]["proposal_hash"], proposal["proposal_hash"])
        self.assertEqual(len(provider.calls), calls)
        self.assertEqual(state["usage"], usage)
        with self.assertRaises(ProjectError):
            manager.accept_patch(proposal["proposal_hash"])
        self.assertEqual(manager.snapshot(), state)

    def test_rejection_changes_no_design_report_tokens_or_version_and_survives_resume(self):
        manager, provider = self.ready()
        before, proposal = self.propose(manager, provider)
        state = manager.reject_patch(proposal["proposal_hash"], "Valeur à confirmer")
        for key in ("design", "preflight", "usage", "design_versions"):
            self.assertEqual(state[key], before[key])
        self.assertEqual(state["patch_proposals"][-1]["decision"]["reason"], "Valeur à confirmer")
        self.assertEqual(ProjectManager(self.directory, Provider()).snapshot(), state)
        with self.assertRaises(ProjectError):
            manager.reject_patch(proposal["proposal_hash"], "Encore")

    def test_stale_tab_and_wrong_hash_cannot_accept_a_different_context(self):
        manager, provider = self.ready()
        _, proposal = self.propose(manager, provider)
        before = manager.snapshot()
        for bad in ("0" * 64, "not-a-hash", None):
            with self.assertRaises(ProjectError):
                manager.accept_patch(bad)
        self.assertEqual(manager.snapshot(), before)
        # Trusted second controller changes the current design while a stale
        # tab retains the first proposal. Recompute no proposal silently.
        manager._state["design"]["root"]["parameters"]["other"] = 1
        record_version(manager.store, manager._state, source="trusted_host_edit")
        manager._save("trusted_host_edit")
        current = manager.snapshot()
        with self.assertRaisesRegex(ProjectError, "context changed"):
            manager.accept_patch(proposal["proposal_hash"])
        self.assertEqual(manager.snapshot(), current)

    def test_changed_brief_or_checks_do_not_silently_accept_old_preview(self):
        manager, provider = self.ready()
        _, proposal = self.propose(manager, provider)
        changed = dict(manager._state["brief"])
        changed["summary"] = "Une autre carte"
        from pcb_dev.project_store import digest
        changed["hash"] = digest({key: value for key, value in changed.items() if key not in {"hash", "approved"}})
        changed["approved"] = False
        manager._state["brief"] = changed
        manager._save("trusted_brief_edit")
        with self.assertRaises(ProjectError):
            manager.accept_patch(proposal["proposal_hash"])
        self.assertFalse(manager.snapshot()["brief"]["approved"])

    def test_atomic_batch_invalid_or_newly_blocking_changes_leave_everything_unchanged(self):
        value = design()
        value["root"]["parameters"] = {"voltage": 3.3}
        value["root"]["constraints"] = [{"expression": "voltage <= 3.6", "message": "Upper bound"}]
        manager, provider = self.ready(value=value)
        before = manager.snapshot()
        state, proposal = self.propose(manager, provider, ops=[operation(3.4), operation(5)])
        self.assertIsNone(proposal)
        for key in ("design", "preflight", "design_versions"):
            self.assertEqual(state[key], before[key])
        self.assertEqual(state["status"], "blocked")

    def test_model_cannot_accept_reject_or_remove_constraints(self):
        manager, provider = self.ready()
        _, proposal = self.propose(manager, provider)
        # Tool is not even present in the allowlist and pending blocks calls.
        with self.assertRaises(ProjectError):
            manager.tools.execute(clone(manager._state), ToolCall("forged", "accept_patch", {"proposal_hash": proposal["proposal_hash"]}))
        self.assertNotIn("accept_patch", {tool.name for tool in provider.calls[-1][1]})
        self.assertNotIn("reject_patch", {tool.name for tool in provider.calls[-1][1]})

    def test_policy_is_persisted_and_omission_never_disables_review(self):
        manager, provider = self.ready()
        _, proposal = self.propose(manager, provider)
        resumed = ProjectManager(self.directory, Provider())
        self.assertTrue(resumed.snapshot()["review_patches"])
        self.assertEqual(resumed.snapshot()["patch_proposals"][-1]["proposal_hash"], proposal["proposal_hash"])
        with self.assertRaisesRegex(ProjectError, "pending patch"):
            ProjectManager(self.directory, Provider(), review_patches=False)
        resumed.reject_patch(proposal["proposal_hash"], "Politique à changer")
        switched = ProjectManager(self.directory, Provider(), review_patches=False)
        self.assertFalse(switched.snapshot()["review_patches"])
        for invalid in ("true", 1, []):
            with self.assertRaises(ProjectError):
                ProjectManager(Path(self.temp.name) / "bad", Provider(), review_patches=invalid)

    def test_historical_tampering_missing_files_and_unlisted_hash_fail_closed(self):
        manager, provider = self.ready()
        before = manager.snapshot()["design_versions"][0]
        _, proposal = self.propose(manager, provider)
        state = manager.accept_patch(proposal["proposal_hash"])
        after = state["design_versions"][-1]
        original = manager.artifact_path(before["relative_path"]).read_bytes()
        path = manager.artifact_path(before["relative_path"])
        path.write_text("{}")
        with self.assertRaises(ValueError):
            manager.compare_versions(before["design_hash"], after["design_hash"])
        path.write_bytes(original)
        with self.assertRaises(ProjectError):
            manager.compare_versions("0" * 64, after["design_hash"])
        path.unlink()
        with self.assertRaises(ProjectError):
            manager.compare_versions(before["design_hash"], after["design_hash"])

    def test_semantic_diff_uses_component_port_child_ids_and_pin_numbers(self):
        value = design()
        value["root"]["children"] = [{"id": "a", "components": [
            {"id": "r2", "reference": "R2", "value": "2k", "footprint": "X:R", "pins": {"2": "GND", "1": "VCC"}},
            {"id": "r1", "reference": "R1", "value": "1k", "footprint": "X:R", "pins": {"1": "VCC", "2": "GND"}}]}, {"id": "b"}]
        first = Design.from_dict(value)
        other = deepcopy(value)
        other["root"]["children"].reverse()
        other["root"]["children"][1]["components"].reverse()
        self.assertEqual(semantic_diff(first, Design.from_dict(other)), [])
        other["root"]["children"][1]["components"][0]["pins"]["1"] = "DATA"
        changes = semantic_diff(first, Design.from_dict(other))
        self.assertEqual(changes, [{"path": "/root/children/a/components/r1/pins/1", "change": "changed", "before": "VCC", "after": "DATA"}])

    def test_acceptance_invalidates_old_native_and_simulation_evidence(self):
        manager, provider = self.ready()
        design_hash = Design.from_dict(manager.snapshot()["design"]).fingerprint()
        for key in ("native_evidence", "simulation_evidence"):
            manager._state[key] = {"status": "passed", "design_sha256": design_hash}
        manager._save("fixture_native_evidence")
        _, proposal = self.propose(manager, provider)
        state = manager.accept_patch(proposal["proposal_hash"])
        self.assertTrue(state["native_evidence"]["stale"])
        self.assertTrue(state["simulation_evidence"]["stale"])
        self.assertEqual(state["status"], "blocked")

    def test_real_legacy_project_upgrade_and_wrong_mode_have_no_premature_write(self):
        manager, _ = self.ready(review=False)
        # Recreate the actual v0.4 envelope policy/history shape, preserving
        # its valid audit rather than inventing a malformed legacy fixture.
        state = clone(manager._state)
        for key in ("review_patches", "patch_proposals", "design_versions"):
            state.pop(key)
        state["artifacts"] = [item for item in state["artifacts"] if not item["relative_path"].startswith("versions/")]
        manager.store.save(state, "legacy_fixture")
        path = self.directory / "project.json"
        before = path.read_bytes()
        from pcb_dev.project import DemoManagerProvider
        with self.assertRaisesRegex(ProjectError, "same explicit"):
            ProjectManager(self.directory, DemoManagerProvider(), review_patches=True)
        self.assertEqual(path.read_bytes(), before)
        upgraded = ProjectManager(self.directory, Provider(), review_patches=True)
        self.assertTrue(upgraded.snapshot()["review_patches"])
        self.assertEqual(len(upgraded.snapshot()["design_versions"]), 1)
        self.assertEqual(ProjectManager(self.directory, Provider()).snapshot(), upgraded.snapshot())

    def test_failed_atomic_version_publication_leaves_no_partial_snapshot_or_commit(self):
        manager, provider = self.ready()
        _, proposal = self.propose(manager, provider)
        before = manager.snapshot()
        destination = manager.directory / "versions" / (proposal["candidate_design_hash"] + ".json")
        with patch("pcb_dev.versions.os.link", side_effect=OSError("filesystem denied link")):
            with self.assertRaisesRegex(ProjectError, "atomically"):
                manager.accept_patch(proposal["proposal_hash"])
        self.assertFalse(destination.exists())
        self.assertEqual(list(destination.parent.glob(".version-*")), [])
        self.assertEqual(manager.snapshot(), before)
        self.assertEqual(manager.accept_patch(proposal["proposal_hash"])["patch_proposals"][-1]["status"], "accepted")

    def test_operation_and_proposal_bounds_fail_before_editing(self):
        manager, provider = self.ready()
        before = manager.snapshot()["design"]
        state, proposal = self.propose(manager, provider, ops=[operation(index) for index in range(33)])
        self.assertIsNone(proposal)
        self.assertEqual(state["design"], before)


if __name__ == "__main__":
    unittest.main()
