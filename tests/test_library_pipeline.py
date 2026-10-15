import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from pcb_dev.artifacts import archive_build, sha256_file, verify_manifest
from pcb_dev.cli import main
from pcb_dev.pipeline import _bundle_libraries, build_preview


FIXTURE = Path(__file__).resolve().parents[1] / "examples" / "library_divider"


class LibraryPipelineTests(unittest.TestCase):
    def test_complete_library_draft_preserves_sources_and_unqualified_status(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "draft"
            result = build_preview(FIXTURE / "design.json", output, bindings=FIXTURE / "bindings.json")
            self.assertEqual(result["kind"], "library-draft")
            self.assertFalse(result["fabrication_ready"])
            manifest = verify_manifest(output)
            self.assertEqual(manifest["kind"], "library-draft")
            lock = json.loads((output / "build-lock.json").read_text(encoding="utf-8"))
            self.assertFalse(lock["fabrication_ready"])
            self.assertEqual(lock["native_checks"], {"erc": "not_run", "drc": "not_run"})
            self.assertTrue(lock["libraries"]["sources"])
            self.assertTrue(lock["libraries"]["licenses"])
            self.assertTrue((output / lock["bundled_bindings"]).is_file())
            board = next((output / "kicad").glob("*.kicad_pcb")).read_text(encoding="utf-8")
            self.assertNotIn("GenericPreview", board)
            self.assertIn("PCBDEV_LIBRARY_DRAFT", board)

    def test_bundled_sources_allow_identical_offline_reconstruction(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            first, second = base / "first", base / "second"
            build_preview(FIXTURE / "design.json", first, bindings=FIXTURE / "bindings.json")
            build_preview(first / "design.json", second, bindings=first / "libraries/bindings.json")
            self.assertEqual(verify_manifest(first), verify_manifest(second))
            archive_build(first, base / "one.zip")
            archive_build(second, base / "two.zip")
            self.assertEqual(sha256_file(base / "one.zip"), sha256_file(base / "two.zip"))

    def test_tampered_source_never_publishes_partial_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "input"
            shutil.copytree(FIXTURE, source)
            path = next((source / "libraries").glob("*.kicad_mod"))
            path.write_bytes(path.read_bytes() + b"\n; source changed\n")
            with self.assertRaises(ValueError):
                build_preview(source / "design.json", base / "output", bindings=source / "bindings.json")
            self.assertFalse((base / "output").exists())

    def test_missing_binding_cannot_fall_back_to_generic_geometry(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "input"
            shutil.copytree(FIXTURE, source)
            path = source / "bindings.json"
            specification = json.loads(path.read_text(encoding="utf-8"))
            specification["components"].pop(next(iter(specification["components"])))
            path.write_text(json.dumps(specification), encoding="utf-8")
            with self.assertRaises(ValueError):
                build_preview(source / "design.json", base / "output", bindings=path)
            self.assertFalse((base / "output").exists())

    def test_unsupported_bottom_placement_is_rejected_before_generation(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            path = base / "design.json"
            design = json.loads((FIXTURE / "design.json").read_text(encoding="utf-8"))
            pending = [design["root"]]
            while pending:
                block = pending.pop()
                for component in block.get("components", []):
                    component["position"]["side"] = "bottom"
                pending.extend(block.get("children", []))
            path.write_text(json.dumps(design), encoding="utf-8")
            with self.assertRaises(ValueError):
                build_preview(path, base / "output", bindings=FIXTURE / "bindings.json")
            self.assertFalse((base / "output").exists())

    def test_snapshot_bundle_uses_validated_bytes_without_rereading_inputs(self):
        from pcb_dev.bindings import load_bindings
        from pcb_dev.model import load_design
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "input"
            shutil.copytree(FIXTURE, source)
            bound = load_bindings(load_design(source / "design.json"), source / "bindings.json")
            shutil.rmtree(source)
            _bundle_libraries(bound, base / "bundle")
            self.assertEqual((base / "bundle/bindings.json").read_bytes(), bound.bindings_content)

    def test_cli_selects_library_mode(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "draft"
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = main(["build", str(FIXTURE / "design.json"), "--bindings", str(FIXTURE / "bindings.json"),
                             "--output", str(output)])
            self.assertEqual(code, 0, stderr.getvalue())
            self.assertIn("Library draft built", stdout.getvalue())
            self.assertEqual(verify_manifest(output)["kind"], "library-draft")
