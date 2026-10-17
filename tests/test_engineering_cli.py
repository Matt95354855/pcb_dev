"""Host options reach actual builds and never become model-controlled tools."""

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pcb_dev.artifacts import verify_manifest
from pcb_dev.cli import _parser, main
from pcb_dev.studio_launch import configured_manager


FIXTURE = Path(__file__).resolve().parents[1] / "examples/decoupling"


class EngineeringCLITests(unittest.TestCase):
    def test_review_policy_is_retained_unless_the_host_selects_a_mode(self):
        base = ["studio", "--project", "ignored", "--demo"]
        self.assertIsNone(_parser().parse_args(base).review_patches)
        self.assertTrue(_parser().parse_args([*base, "--review-patches"]).review_patches)
        self.assertFalse(_parser().parse_args([*base, "--auto-patches"]).review_patches)
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            _parser().parse_args([*base, "--review-patches", "--auto-patches"])

    def test_studio_passes_host_review_and_hierarchy_options_without_inference(self):
        args = _parser().parse_args(["studio", "--project", "ignored", "--demo", "--review-patches", "--hierarchical"])
        with patch("pcb_dev.project.ProjectManager") as manager:
            configured_manager(args)
        self.assertTrue(manager.call_args.kwargs["review_patches"])
        self.assertTrue(manager.call_args.kwargs["hierarchical"])

    def test_public_build_cli_creates_complete_hierarchy_with_real_pinned_capacitor(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "draft"
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                result = main(["build", str(FIXTURE / "design.json"), "--bindings", str(FIXTURE / "bindings.json"),
                               "--hierarchical", "--require-physical-ports", "--output", str(output)])
            self.assertEqual(result, 0)
            lock = json.loads((output / "build-lock.json").read_text(encoding="utf-8"))
            self.assertEqual(lock["schematic_layout"], "hierarchical-global-nets")
            sheets = [name for key, name in lock["generated"].items() if key.startswith("schematic")]
            self.assertEqual(len(sheets), 4)
            self.assertTrue(all((output / name).is_file() for name in sheets))
            self.assertFalse(lock["fabrication_ready"])
            self.assertTrue(verify_manifest(output)["files"])


if __name__ == "__main__":
    unittest.main()
