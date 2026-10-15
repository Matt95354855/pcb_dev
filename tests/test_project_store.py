import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pcb_dev.project_store import ProjectBusyError, ProjectError, ProjectStore


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = ProjectStore(Path(self.temp.name) / "project")

    def state(self):
        return {"schema_version": 1, "revision": 0, "fabrication_ready": False, "conversation": []}

    def test_atomic_audit_keeps_single_snapshot_and_detects_tampering(self):
        state = self.store.initialize(self.state())
        for index in range(30):
            state["conversation"].append({"content": str(index) * 100})
            state = self.store.save(state, "message")
        envelope = json.loads(self.store.safe_path("project.json").read_text(encoding="utf-8"))
        self.assertEqual(len(envelope["events"]), 30)
        self.assertTrue(all("state" not in event for event in envelope["events"]))
        self.assertEqual(self.store.load(), state)
        state["conversation"].append("external")
        self.assertNotEqual(self.store.load(), state)
        envelope["events"][1]["kind"] = "tampered"
        self.store.safe_path("project.json").write_text(json.dumps(envelope), encoding="utf-8")
        with self.assertRaisesRegex(ProjectError, "audit"):
            self.store.load()

    def test_partial_save_failure_preserves_last_readable_state(self):
        state = self.store.initialize(self.state())
        changed = copy.deepcopy(state)
        changed["conversation"].append("new")
        with patch("pcb_dev.project_store.atomic_write_json", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.store.save(changed, "message")
        self.assertEqual(self.store.load(), state)

    def test_storage_bound_refuses_before_replace_and_can_resume(self):
        state = self.store.initialize(self.state())
        changed = copy.deepcopy(state)
        changed["conversation"] = ["x" * 16_000_000]
        with self.assertRaisesRegex(ProjectError, "bounded storage"):
            self.store.save(changed, "oversized")
        self.assertEqual(ProjectStore(self.store.directory).load(), state)
        self.assertEqual(self.store.save(state, "continue")["revision"], 1)

    def test_concurrent_writer_and_stale_revision_are_refused(self):
        state = self.store.initialize(self.state())
        with self.store.locked():
            with self.assertRaises(ProjectBusyError):
                with ProjectStore(self.store.directory).locked():
                    self.fail("second process-level lock admitted")
        newer = self.store.save(state, "first")
        with self.assertRaisesRegex(ProjectError, "changed"):
            self.store.save(state, "stale")
        self.assertEqual(self.store.load(), newer)

    def test_paths_and_symlinks_are_refused(self):
        for spelling in ("../outside", "/absolute", "a/../b", "a//b", "a/./b", "C:/outside", "a\\b", ""):
            with self.subTest(path=spelling), self.assertRaises(ProjectError):
                self.store.safe_path(spelling, create_parent=True)
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        try:
            (self.store.directory / "link").symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation unavailable")
        with self.assertRaisesRegex(ProjectError, "Symlinks"):
            self.store.safe_path("link/escaped.json", create_parent=True)
        alias = Path(self.temp.name) / "alias"
        alias.symlink_to(self.store.directory, target_is_directory=True)
        with self.assertRaisesRegex(ProjectError, "symlink"):
            ProjectStore(alias)
