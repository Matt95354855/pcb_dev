"""Shared evidence-chain tests with explicit doubles, never native execution."""

import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from pcb_dev.backends import generate_bound
from pcb_dev.bindings import load_bindings
from pcb_dev.model import load_design
from pcb_dev.native_validation import run_native_validation
from pcb_dev.reports import CheckReport
from pcb_dev.checks import check_design


FIXTURE = Path(__file__).resolve().parents[1] / "examples/library_divider"


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class NativeValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.design = load_design(FIXTURE / "design.json")
        self.bound = load_bindings(self.design, FIXTURE / "bindings.json")
        self.files = generate_bound(self.bound, self.root / "source")
        xml = ET.fromstring(self.files["netlist"].read_bytes())
        for pin in xml.findall("libparts/libpart/pins/pin"):
            if pin.get("name") == "~":
                pin.set("name", "")
        for node in xml.findall("nets/net/node"):
            if node.get("pinfunction") == "~":
                node.attrib.pop("pinfunction")
        self.xml = ET.tostring(xml, encoding="utf-8", xml_declaration=True)

    def backend(self, change_schematic=False):
        content = self.xml

        class ExplicitBackendDouble:
            def check(self, board, schematic, output):
                return {stage: {"status": "passed", "executed": True,
                    "reason": "Explicit test double; no native execution",
                    "native_report": {"violations": []}, "input_sha256": digest(path)}
                    for stage, path in (("erc", schematic), ("drc", board))}

            def export_netlist(self, schematic, output):
                before = digest(schematic)
                Path(output).write_bytes(content)
                if change_schematic:
                    with Path(schematic).open("ab") as stream:
                        stream.write(b"\n ")
                return {"status": "passed", "executed": True, "input_sha256": before,
                        "output_sha256": digest(output), "path": str(output)}

        return ExplicitBackendDouble()

    def run_gate(self, backend):
        return run_native_validation(self.design, self.files["board"], self.files["schematic"],
                                     self.root / "review", bindings=self.bound, backend=backend)[0]

    def test_export_then_schematic_change_invalidates_current_evidence(self):
        result = self.run_gate(self.backend(change_schematic=True))
        parity = next(report for report in result["reports"] if report["stage"] == "model-parity")
        self.assertFalse(result["validation_passed"])
        self.assertIn("parity.schematic_changed_after_export", {issue["code"] for issue in parity["issues"]})
        self.assertFalse(result["fabrication_ready"])

    def test_geometry_report_from_different_pcb_cannot_pass_native_gate(self):
        preflight = check_design(self.design)
        preflight.metadata["decoupling"] = {"rule_count": 1}
        layout = CheckReport("decoupling-layout", metadata={"completed": True,
            "design_hash": self.design.fingerprint(), "board_sha256": "f" * 64,
            "scope": "explicit evidence-chain double; no geometry execution"})
        with patch("pcb_dev.native_validation.check_design", return_value=preflight), \
                patch("pcb_dev.decoupling.check_layout", return_value=layout):
            result = self.run_gate(self.backend())
        self.assertFalse(result["validation_passed"])
        checked = next(report for report in result["reports"] if report["stage"] == "decoupling-layout")
        self.assertIn("decoupling.board_changed", {issue["code"] for issue in checked["issues"]})

    def test_preexisting_output_is_never_overwritten(self):
        destination = self.root / "review"
        destination.mkdir()
        sentinel = destination / "keep.txt"
        sentinel.write_text("preserve", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.run_gate(self.backend())
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserve")


if __name__ == "__main__":
    unittest.main()
