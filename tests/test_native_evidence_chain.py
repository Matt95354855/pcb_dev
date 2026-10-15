"""Export/parity digest races with explicit doubles; no native EDA execution.

The files use real pinned library geometry, but ERC/DRC and export evidence in
these tests are deliberately synthetic. Native CI provides independent proof.
"""

import contextlib
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from pcb_dev.backends import generate_bound
from pcb_dev.bindings import load_bindings
from pcb_dev.cli import main
from pcb_dev.model import load_design


FIXTURE = Path(__file__).resolve().parents[1] / "examples" / "library_divider"


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class NativeEvidenceChainTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.design = load_design(FIXTURE / "design.json")
        bound = load_bindings(self.design, FIXTURE / "bindings.json")
        self.files = generate_bound(bound, self.root / "source")
        native_xml = ET.fromstring(self.files["netlist"].read_bytes())
        # The only source-to-native normalization required by this no-NC fixture:
        # KiCad represents an unnamed '~' source pin as an empty XML pin name.
        for pin in native_xml.findall("libparts/libpart/pins/pin"):
            if pin.get("name") == "~":
                pin.set("name", "")
        for node in native_xml.findall("nets/net/node"):
            if node.get("pinfunction") == "~":
                node.attrib.pop("pinfunction")
        self.exported_bytes = ET.tostring(native_xml, encoding="utf-8", xml_declaration=True)

    def run_native(self, *, bound_mode, changed_export):
        fixture_bytes = self.exported_bytes

        class ExplicitBackendDouble:
            def check(self, board, schematic, output):
                return {
                    "erc": {"status": "passed", "executed": True,
                            "reason": "Explicit ERC test double; no KiCad execution",
                            "native_report": {"violations": []}, "input_sha256": digest(schematic)},
                    "drc": {"status": "failed", "executed": True,
                            "reason": "Explicit DRC test double: expected unrouted fixture",
                            "native_report": {"violations": [], "unconnected_items": [{"type": "unconnected_items"}]},
                            "input_sha256": digest(board)},
                }

            def export_netlist(self, schematic, output):
                exported_digest = hashlib.sha256(fixture_bytes).hexdigest()
                Path(output).write_bytes(fixture_bytes)
                if changed_export:
                    # Simulate the interval after export captured its hash and
                    # before parity reads the file. Connectivity still matches,
                    # so only the evidence-chain check can detect these bytes.
                    Path(output).write_bytes(fixture_bytes + b"\n<!-- changed after native export snapshot -->\n")
                return {"status": "passed", "executed": True,
                        "reason": "Explicit native-export test double; no KiCad execution",
                        "input_sha256": digest(schematic), "output_sha256": exported_digest,
                        "path": str(output)}

        output = self.root / f"review-{'bound' if bound_mode else 'general'}-{'race' if changed_export else 'stable'}"
        arguments = ["native-check", str(FIXTURE / "design.json"),
                     "--board", str(self.files["board"]), "--schematic", str(self.files["schematic"]),
                     "--output", str(output), "--json"]
        if bound_mode:
            arguments.extend(["--bindings", str(FIXTURE / "bindings.json")])
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("pcb_dev.cli.KiCadBackend", return_value=ExplicitBackendDouble()), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status = main(arguments)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(status, 1)
        result = json.loads(stdout.getvalue())
        self.assertEqual(result, json.loads((output / "checks.json").read_text(encoding="utf-8")))
        self.assertFalse(result["validation_passed"])
        self.assertFalse(result["fabrication_ready"])
        reports = {report["stage"]: report for report in result["reports"]}
        self.assertTrue(reports["preflight"]["passed"])
        self.assertTrue(reports["erc"]["passed"])
        self.assertFalse(reports["drc"]["passed"])
        return result, reports["model-parity"]

    def test_coherent_native_export_digest_keeps_only_expected_unrouted_drc_blocker(self):
        for bound_mode in (False, True):
            with self.subTest(bound_mode=bound_mode):
                result, parity = self.run_native(bound_mode=bound_mode, changed_export=False)
                self.assertTrue(parity["passed"], parity)
                self.assertTrue(parity["metadata"]["completed"])
                self.assertEqual(parity["metadata"]["netlist_sha256"],
                                 parity["metadata"]["native_netlist_export"]["output_sha256"])
                self.assertEqual([issue["block_path"] for issue in result["gate"]["issues"]], ["drc"])

    def test_changed_export_bytes_block_parity_even_when_component_pin_nets_still_match(self):
        for bound_mode in (False, True):
            with self.subTest(bound_mode=bound_mode):
                result, parity = self.run_native(bound_mode=bound_mode, changed_export=True)
                self.assertTrue(parity["metadata"]["completed"])
                self.assertNotEqual(parity["metadata"]["netlist_sha256"],
                                    parity["metadata"]["native_netlist_export"]["output_sha256"])
                self.assertFalse(parity["passed"])
                self.assertEqual([issue["code"] for issue in parity["issues"]], ["parity.netlist_changed"])
                failed_stages = {issue["block_path"] for issue in result["gate"]["issues"]}
                self.assertEqual(failed_stages, {"drc", "model-parity"})


if __name__ == "__main__":
    unittest.main()
