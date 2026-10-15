"""Offline fixture/acceptance tests; these do not claim native KiCad execution."""

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from pcb_dev.backends.smoke_libraries import run_smoke, verify_board_geometry, verify_native_netlist
from pcb_dev.bindings import load_bindings
from pcb_dev.libraries import load_pinned_library, load_registry
from pcb_dev.model import load_design


FIXTURE = Path(__file__).resolve().parents[1] / "examples/library_divider"
SYMBOL_COMMIT = "a9012d8971ef2ea3243ffc9f19a5b4f153d8b334"
FOOTPRINT_COMMIT = "e515f1c3213317d436e492c8c06620ef4caca84e"
SOURCE_HASHES = {
    "symbol-device": "972f851ebf7c10dfe97eefd5d847406154b18ee6158013bd19165477d6364abd",
    "symbol-connectors": "c44a0a561694b37a58027539b4e215067f64b5789cd7855bc001890dc484d51b",
    "footprint-r0603": "4704c9ccd300f50c721cdc6a7a1f3e06f9f1df0b6142a98b8600d00de1182a28",
    "footprint-header": "ec752bbcecf855e3d852409e516965b03a5a461abf45002765cb482266cdf973",
}


class NativeLibraryContractTests(unittest.TestCase):
    def test_official_sources_have_exact_commits_hashes_and_documented_derivation(self):
        provenance = json.loads((FIXTURE / "SOURCE-PROVENANCE.json").read_text(encoding="utf-8"))
        registry = load_registry(FIXTURE / "registry.json")
        pins = {pin.name: pin for pin in registry.libraries}
        self.assertEqual(set(pins), set(SOURCE_HASHES))
        for source in provenance["sources"]:
            alias = source["alias"]
            self.assertEqual(source["upstream_sha256"], SOURCE_HASHES[alias])
            expected_commit = SYMBOL_COMMIT if alias.startswith("symbol-") else FOOTPRINT_COMMIT
            self.assertEqual(source["upstream_commit"], expected_commit)
            self.assertIn(f"/-/raw/{expected_commit}/", source["upstream_url"])
            raw = (FIXTURE / source["file"]).read_bytes()
            self.assertEqual(hashlib.sha256(raw).hexdigest(), source["sha256"])
            self.assertEqual(pins[alias].sha256, source["sha256"])
            self.assertEqual(source["license"], "CC-BY-SA-4.0 WITH KiCad-Libraries-Exception")
            if alias.startswith("symbol-"):
                self.assertNotEqual(source["upstream_sha256"], source["sha256"])
                self.assertIn("Extracted", source["modification"])
            else:
                self.assertEqual(source["upstream_sha256"], source["sha256"])

    def test_vendored_license_and_attribution_are_captured_as_binding_sources(self):
        design = load_design(FIXTURE / "design.json")
        bound = load_bindings(design, FIXTURE / "bindings.json")
        bindings = json.loads((FIXTURE / "bindings.json").read_text(encoding="utf-8"))
        self.assertEqual(set(bindings["licenses"]), {"ATTRIBUTION.md", "KICAD-LIBRARIES-LICENSE.md", "CC-BY-SA-4.0.txt", "SOURCE-PROVENANCE.json"})
        license_text = (FIXTURE / "KICAD-LIBRARIES-LICENSE.md").read_text(encoding="utf-8")
        self.assertIn("CC-BY-SA 4.0", license_text)
        self.assertIn("waives article 3", license_text)
        self.assertIn("Creative Commons Attribution-ShareAlike 4.0", (FIXTURE / "CC-BY-SA-4.0.txt").read_text(encoding="utf-8"))
        self.assertIn("KiCad community", (FIXTURE / "ATTRIBUTION.md").read_text(encoding="utf-8"))
        self.assertEqual(set(bound.components), {"board.j1", "board.r1", "board.r2"})
        for source in json.loads((FIXTURE / "SOURCE-PROVENANCE.json").read_text(encoding="utf-8"))["license_sources"]:
            self.assertEqual(hashlib.sha256((FIXTURE / source["file"]).read_bytes()).hexdigest(), source["sha256"])

    def test_minimal_real_symbols_and_mixed_real_pad_geometry_are_present(self):
        registry = FIXTURE / "registry.json"
        resistors = load_pinned_library(registry, "symbol-device")
        connectors = load_pinned_library(registry, "symbol-connectors")
        self.assertEqual([symbol.name for symbol in resistors.symbols], ["R"])
        self.assertEqual([symbol.name for symbol in connectors.symbols], ["Conn_01x02"])
        self.assertEqual({pin.electrical_type for pin in resistors.symbols[0].pins}, {"passive"})
        self.assertEqual([pin.name for pin in connectors.symbols[0].pins], ["Pin_1", "Pin_2"])
        resistor = load_pinned_library(registry, "footprint-r0603")
        header = load_pinned_library(registry, "footprint-header")
        self.assertTrue(resistor.courtyard_present and header.courtyard_present)
        self.assertEqual([(pad.type, pad.shape, pad.size_x_mm, pad.size_y_mm) for pad in resistor.pads],
                         [("smd", "roundrect", .8, .95), ("smd", "roundrect", .8, .95)])
        self.assertEqual([(pad.type, pad.drill_x_mm) for pad in header.pads], [("thru_hole", 1), ("thru_hole", 1)])
        self.assertEqual(header.pads[1].y_mm, 2.54)

    def test_native_smoke_refuses_to_skip_an_unavailable_real_engine(self):
        with tempfile.TemporaryDirectory() as directory, patch("pcb_dev.backends.smoke_libraries.KiCadBackend.diagnose", return_value={"supported": False, "status": "unavailable"}):
            output = Path(directory) / "native"
            result = run_smoke(output, FIXTURE)
            self.assertEqual(result["status"], "failed")
            self.assertFalse(result["fabrication_ready"])
            self.assertIn("mandatory", result["reason"])
            self.assertNotIn("claims", result)
            self.assertFalse((output / "source").exists())
            self.assertEqual(json.loads((output / "smoke-report.json").read_text(encoding="utf-8"))["status"], "failed")

    def test_generated_real_geometry_local_rotations_are_checked_without_claiming_native_load(self):
        from pcb_dev.backends.library_kicad import generate_bound
        design = load_design(FIXTURE / "design.json")
        bound = load_bindings(design, FIXTURE / "bindings.json")
        with tempfile.TemporaryDirectory() as directory:
            files = generate_bound(bound, Path(directory) / "generated")
            report = verify_board_geometry(bound, files["board"])
            self.assertEqual(report["status"], "passed")
            self.assertFalse(report["native"])
            self.assertEqual([item["rotation_deg"] for item in report["components"]], [90, 90, 270])
            text = files["board"].read_text(encoding="utf-8")
            self.assertIn("roundrect", text)
            files["board"].write_text(text.replace("roundrect", "rect", 1), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "shape was replaced"):
                verify_board_geometry(bound, files["board"])

    def test_native_netlist_acceptance_rejects_incorrect_real_pin_type(self):
        # Deliberately authored XML tests the acceptance checker only; it is not
        # evidence of running KiCad. Actual integration runs use native exports.
        bound = load_bindings(load_design(FIXTURE / "design.json"), FIXTURE / "bindings.json")
        root = ET.Element("export")
        components, libparts, nets = (ET.SubElement(root, name) for name in ("components", "libparts", "nets"))
        seen = set()
        net_nodes = {}
        for item in bound.components.values():
            component = ET.SubElement(components, "comp", ref=item.component.reference)
            library, part = item.symbol_id.split(":", 1)
            ET.SubElement(component, "libsource", lib=library, part=part)
            if item.symbol_id not in seen:
                seen.add(item.symbol_id)
                pins = ET.SubElement(ET.SubElement(libparts, "libpart", lib=library, part=part), "pins")
                for pin in item.symbol_inventory.pins:
                    ET.SubElement(pins, "pin", num=pin.number, name="" if pin.name == "~" else pin.name, type=pin.electrical_type)
            for number, net in item.component.pins.items():
                if net not in net_nodes:
                    net_nodes[net] = ET.SubElement(nets, "net", name=net)
                ET.SubElement(net_nodes[net], "node", ref=item.component.reference, pin=number)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "unit-test-only.net"
            ET.ElementTree(root).write(path, encoding="utf-8")
            self.assertEqual(verify_native_netlist(bound, path)["status"], "passed")
            libparts.find("libpart/pins/pin").set("type", "power_in")
            ET.ElementTree(root).write(path, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "pin name/type"):
                verify_native_netlist(bound, path)
            libparts.find("libpart/pins/pin").set("type", "passive")
            libparts.find("libpart/pins/pin").set("name", "renamed-real-pin")
            ET.ElementTree(root).write(path, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "pin name/type"):
                verify_native_netlist(bound, path)


if __name__ == "__main__":
    unittest.main()
