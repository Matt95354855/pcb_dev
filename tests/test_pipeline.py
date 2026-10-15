import contextlib
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path

from pcb_dev.artifacts import ArtifactError, archive_build, sha256_file, verify_manifest
from pcb_dev.cli import main
from pcb_dev.fabrication import bom_csv, placement_csv
from pcb_dev.model import Design, load_design
from pcb_dev.pipeline import BuildError, build_preview


EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "usb_mcu_led.json"


class PipelineTests(unittest.TestCase):
    def test_builds_have_identical_bytes_and_archives(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            for name in ("one", "two"):
                result = build_preview(EXAMPLE, base / name)
                self.assertFalse(result["fabrication_ready"])
                manifest = verify_manifest(base / name)
                self.assertEqual(manifest["kind"], "preview")
                self.assertGreater(len(manifest["files"]), 10)
                archive_build(base / name, base / f"{name}.zip")
            self.assertEqual(sha256_file(base / "one.zip"), sha256_file(base / "two.zip"))

    def test_model_error_blocks_all_preview_artifacts(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "blocked"
            with self.assertRaises(BuildError):
                build_preview(EXAMPLE.with_name("invalid_voltage.json"), output)
            self.assertFalse(output.exists())

    def test_existing_outputs_and_source_ancestors_are_protected(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            existing = base / "existing"
            existing.mkdir()
            (existing / "keep.txt").write_text("keep")
            with self.assertRaises(BuildError):
                build_preview(EXAMPLE, existing)
            self.assertEqual((existing / "keep.txt").read_text(), "keep")
        with self.assertRaises(BuildError):
            build_preview(EXAMPLE, EXAMPLE.parent)

    def test_integrity_detects_tampering_and_untracked_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "demo"
            build_preview(EXAMPLE, output)
            (output / "bom.csv").write_text("tampered")
            with self.assertRaises(ArtifactError):
                verify_manifest(output)
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "demo"
            build_preview(EXAMPLE, output)
            (output / "untracked.txt").write_text("unexpected")
            with self.assertRaises(ArtifactError):
                verify_manifest(output)

    def test_archive_path_cannot_contaminate_build(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "demo"
            build_preview(EXAMPLE, output)
            with self.assertRaises(ArtifactError):
                archive_build(output, output / "archive.zip")
            archive = Path(temporary) / "preview.zip"
            archive_build(output, archive)
            with self.assertRaises(ArtifactError):
                archive_build(output, archive)

    def test_manifest_path_traversal_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            (output / "manifest.json").write_text(json.dumps({"schema_version": 1,
                "files": {"../outside": {"sha256": "f" * 64, "size": 1}}}))
            with self.assertRaises(ArtifactError):
                verify_manifest(output)

    def test_viewer_escapes_design_content(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            data = copy.deepcopy(load_design(EXAMPLE).to_dict())
            data["name"] = '</script><script>alert("injection")</script>'
            source = base / "design.json"
            source.write_text(json.dumps(data))
            build_preview(source, base / "preview")
            viewer = (base / "preview" / "index.html").read_text()
            self.assertNotIn(data["name"], viewer)
            self.assertIn("\\u003c/script\\u003e", viewer)

    def test_bom_dnp_and_placement_are_explicit(self):
        data = load_design(EXAMPLE).to_dict()
        data["root"]["children"][0]["components"][1]["fitted"] = False
        data["root"]["children"][0]["components"][0]["position"] = None
        design = Design.from_dict(data)
        self.assertNotIn("R1", bom_csv(design))
        self.assertNotIn("J1", placement_csv(design))
        with self.assertRaises(ValueError):
            placement_csv(design, strict=True)


class CLITests(unittest.TestCase):
    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(list(map(str, args)))
        return code, out.getvalue(), err.getvalue()

    def test_check_success_and_failure(self):
        code, out, err = self.run_cli("check", EXAMPLE, "--json")
        self.assertEqual(code, 0, err)
        self.assertFalse(json.loads(out)["blocking"])
        code, out, err = self.run_cli("check", EXAMPLE.with_name("invalid_power_budget.json"), "--json")
        self.assertEqual(code, 1, err)
        self.assertTrue(json.loads(out)["blocking"])

    def test_graph_inspect_and_bad_input(self):
        code, out, err = self.run_cli("graph", EXAMPLE, "--impact", "board.power", "--json")
        self.assertEqual(code, 0, err)
        self.assertEqual(set(json.loads(out)["affected"]), {"board.mcu", "board.led"})
        code, out, err = self.run_cli("inspect", EXAMPLE)
        self.assertEqual(code, 0, err)
        self.assertAlmostEqual(json.loads(out)["parameters"]["board.power"]["dissipation_w"], 0.153)
        code, _, err = self.run_cli("check", "/nonexistent-pcb-design.json")
        self.assertEqual(code, 2)
        self.assertIn("pcb-dev:", err)

    def test_freeze_context_roundtrip(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "frozen.json"
            code, _, err = self.run_cli("freeze", EXAMPLE, "board.mcu", "--output", output)
            self.assertEqual(code, 0, err)
            code, out, err = self.run_cli("check", output, "--json")
            self.assertEqual(code, 0, err)
            self.assertFalse(json.loads(out)["blocking"])

    def test_report_cannot_overwrite_input(self):
        code, _, err = self.run_cli("check", EXAMPLE, "--output", EXAMPLE)
        self.assertEqual(code, 2)
        self.assertIn("overwrite", err)

    def test_offline_agent_cli_preserves_source_and_writes_proposal(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "agent"
            before = EXAMPLE.read_bytes()
            code, out, err = self.run_cli("agent-demo", EXAMPLE,
                "--script", EXAMPLE.with_name("agent_session.json"), "--output", output)
            self.assertEqual(code, 0, err)
            self.assertIn("model_finished", out)
            self.assertEqual(EXAMPLE.read_bytes(), before)
            proposed = load_design(output / "proposed-design.json")
            self.assertEqual(dict(proposed.walk_blocks())["board.led"].parameters["led_forward_v"], 2.1)
            self.assertFalse(json.loads((output / "session.json").read_text())["fabrication_ready"])

    def test_remote_agent_requires_explicit_flag_before_network(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            config = base / "config.json"
            config.write_text(json.dumps({"profile": {"model": "example"},
                "endpoint": "https://example.com/v1", "offline": False, "allow_network": True}))
            code, _, err = self.run_cli("agent-run", EXAMPLE, "--config", config,
                "--objective", "Review", "--output", base / "proposal")
            self.assertEqual(code, 2)
            self.assertIn("--allow-network", err)
            self.assertFalse((base / "proposal").exists())
