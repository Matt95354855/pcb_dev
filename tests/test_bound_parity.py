"""Explicit NC normalization requires identity/type/membership evidence.

XML mutations below are synthetic contract fixtures, not native KiCad runs.
The independent smoke_no_connect module requires a real native executable.
"""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from pcb_dev.backends import generate_bound
from pcb_dev.bindings import load_bindings
from pcb_dev.bound_parity import check_bound_parity
from pcb_dev.model import load_design
from pcb_dev.parity import check_parity, parse_netlist
from pcb_dev.sexpr import Quoted, dumps, parse_quoted


FIXTURE = Path(__file__).resolve().parents[1] / "examples/library_divider"


def children(node, key):
    return [item for item in node[1:] if isinstance(item, list) and item and item[0] == key]


class BoundParityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.fixture = self.root / "fixture"
        shutil.copytree(FIXTURE, self.fixture)
        self.design = load_design(self.fixture / "design.json")
        del self.design.root.components[1].pins["2"]
        data = json.loads((self.fixture / "bindings.json").read_text())
        data["components"]["board.r1"]["no_connect_pins"] = ["2"]
        (self.fixture / "bindings.json").write_text(json.dumps(data))
        self.bound = load_bindings(self.design, self.fixture / "bindings.json")
        self.paths = generate_bound(self.bound, self.root / "output")
        self.xml = ET.fromstring(self.paths["netlist"].read_text())
        # Native encoding of the source library's unnamed pin placeholder.
        for pin in self.xml.findall("libparts/libpart/pins/pin"):
            if pin.get("name") == "~":
                pin.set("name", "")
        for node in self.xml.findall("nets/net/node"):
            if node.get("pinfunction") == "~":
                del node.attrib["pinfunction"]
        net = ET.SubElement(self.xml.find("nets"), "net", code="99", name="unconnected-(R1-Pad2)")
        self.nc_node = ET.SubElement(net, "node", ref="R1", pin="2", pintype="passive+no_connect")
        self.board = parse_quoted(self.paths["board"].read_text())

    def tearDown(self):
        self.temporary.cleanup()

    def check(self):
        ET.indent(self.xml)
        self.paths["netlist"].write_text(ET.tostring(self.xml, encoding="unicode"))
        self.paths["board"].write_text(dumps(self.board))
        return check_bound_parity(self.bound, self.paths["netlist"], self.paths["board"])

    def pad(self, reference="R1", number="2"):
        for footprint in children(self.board, "footprint"):
            ref = next(field[2] for field in children(footprint, "property") if field[1] == "Reference")
            if ref == reference:
                return next(pad for pad in children(footprint, "pad") if pad[1] == number)
        raise AssertionError("fixture pad missing")

    def nc_pair_fixture(self, *, stacked):
        """Build a separately pinned synthetic symbol with two declared NCs."""
        self.design.root.components = [self.design.root.components[1]]
        self.design.root.components[0].pins = {}
        data = json.loads((self.fixture / "bindings.json").read_text())
        entry = data["components"]["board.r1"]
        entry["no_connect_pins"] = ["1", "2"]
        data["components"] = {"board.r1": entry}
        (self.fixture / "bindings.json").write_text(json.dumps(data))
        if stacked:
            source = self.fixture / "libraries/Device.kicad_sym"
            tree = parse_quoted(source.read_text())
            symbol = children(tree, "symbol")[0]
            pins = [pin for unit in children(symbol, "symbol") for pin in children(unit, "pin")]
            children(pins[1], "at")[0][1:] = children(pins[0], "at")[0][1:]
            source.write_text(dumps(tree))
            registry = json.loads((self.fixture / "registry.json").read_text())
            record = next(item for item in registry["libraries"] if item["name"] == "symbol-device")
            record["sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
            (self.fixture / "registry.json").write_text(json.dumps(registry))
        self.bound = load_bindings(self.design, self.fixture / "bindings.json")
        self.paths = generate_bound(self.bound, self.root / "pair-output")
        self.xml = ET.fromstring(self.paths["netlist"].read_text())
        for pin in self.xml.findall("libparts/libpart/pins/pin"):
            if pin.get("name") == "~":
                pin.set("name", "")
        net = ET.SubElement(self.xml.find("nets"), "net", code="99", name="unconnected-stacked-fixture")
        for number in ("1", "2"):
            ET.SubElement(net, "node", ref="R1", pin=number, pintype="passive+no_connect")
        self.board = parse_quoted(self.paths["board"].read_text())

    def test_explicit_singleton_nc_passes_without_claiming_native_or_copper_evidence(self):
        report = self.check()
        self.assertTrue(report.passed, report.to_json())
        self.assertTrue(report.metadata["completed"])
        self.assertFalse(report.metadata["native"])
        self.assertFalse(report.metadata["copper_connectivity_verified"])
        self.assertFalse(report.metadata["physical_footprints_qualified"])
        self.assertEqual(report.metadata["no_connect_pin_count"], 1)
        self.assertEqual(report.metadata["no_connects"][0]["pins"], [["R1", "2"]])
        self.assertEqual(report.metadata["design_hash"], self.design.fingerprint())
        self.assertEqual(len(report.metadata["netlist_sha256"]), 64)

    def test_default_parity_and_parser_keep_the_nc_placeholder_unchanged(self):
        self.assertTrue(self.check().passed)
        parsed = parse_netlist(self.paths["netlist"])
        self.assertEqual(parsed["R1"]["pins"]["2"], "unconnected-(R1-Pad2)")
        self.assertTrue(check_parity(self.design, self.paths["netlist"], self.paths["board"]).blocking)

    def test_nc_acceptance_is_by_explicit_identity_not_net_name_prefix(self):
        self.xml.find("nets/net[@code='99']").set("name", "different-native-singleton-name")
        self.assertTrue(self.check().passed)

    def test_kicad10_named_node_function_encoding_is_exactly_source_name_and_pin(self):
        for node in self.xml.findall("nets/net/node"):
            if node.get("ref") == "J1":
                node.set("pinfunction", node.get("pinfunction") + "_" + node.get("pin"))
        self.assertTrue(self.check().passed)
        self.xml.find("nets/net/node[@ref='J1'][@pin='1']").set("pinfunction", "Pin_1_777")
        self.assertTrue(self.check().blocking)

    def test_missing_marker_wrong_type_or_intrinsic_nc_is_not_an_explicit_nc(self):
        for pin_type in (None, "passive", "no_connect", "input+no_connect", "passive+no_connect+no_connect"):
            with self.subTest(pin_type=pin_type):
                if pin_type is None:
                    self.nc_node.attrib.pop("pintype", None)
                else:
                    self.nc_node.set("pintype", pin_type)
                self.assertTrue(self.check().blocking)

    def test_missing_nc_node_and_nc_copper_net_are_rejected(self):
        net = self.xml.find("nets/net[@code='99']")
        net.remove(self.nc_node)
        self.assertTrue(self.check().blocking)
        net.append(self.nc_node)
        self.pad().append(["net", "1", Quoted("GND")])
        self.assertTrue(self.check().blocking)

    def test_allocated_pcb_placeholder_nc_net_is_rejected_in_this_version(self):
        self.board.append(["net", "99", Quoted("unconnected-(R1-Pad2)")])
        self.pad().append(["net", "99", Quoted("unconnected-(R1-Pad2)")])
        self.assertTrue(self.check().blocking)

    def test_explicit_zero_empty_pcb_net_is_supported(self):
        self.pad().append(["net", "0", Quoted("")])
        self.assertTrue(self.check().passed)

    def test_missing_nc_pad_is_rejected_even_though_general_parser_omits_it(self):
        pad = self.pad()
        footprint = next(item for item in children(self.board, "footprint") if pad in item)
        footprint.remove(pad)
        self.assertTrue(self.check().blocking)

    def test_changed_pad_type_or_duplicate_nc_pad_is_rejected(self):
        pad = self.pad()
        pad[2] = "thru_hole"
        self.assertTrue(self.check().blocking)
        pad[2] = "smd"
        footprint = next(item for item in children(self.board, "footprint") if pad in item)
        footprint.append(deepcopy(pad))
        self.assertTrue(self.check().blocking)

    def test_connected_pin_cannot_be_dropped_or_marked_nc(self):
        node = self.xml.find("nets/net/node[@ref='R1'][@pin='1']")
        node.set("pintype", "passive+no_connect")
        self.assertTrue(self.check().blocking)
        node.set("pintype", "passive")
        net = next(net for net in self.xml.findall("nets/net") if node in list(net))
        net.remove(node)
        self.assertTrue(self.check().blocking)

    def test_connected_net_mutations_in_either_source_are_rejected(self):
        node = self.xml.find("nets/net/node[@ref='R1'][@pin='1']")
        current = next(net for net in self.xml.findall("nets/net") if node in list(net))
        target = next(net for net in self.xml.findall("nets/net") if net.get("name") == "GND")
        current.remove(node)
        target.append(node)
        self.assertTrue(self.check().blocking)
        target.remove(node)
        current.append(node)
        pad = self.pad(number="1")
        children(pad, "net")[0][1:] = ["1", Quoted("GND")]
        self.assertTrue(self.check().blocking)

    def test_nc_shared_with_a_connected_pin_is_rejected(self):
        net = self.xml.find("nets/net[@code='99']")
        net.remove(self.nc_node)
        next(item for item in self.xml.findall("nets/net") if item.get("name") == "GND").append(self.nc_node)
        self.assertTrue(self.check().blocking)

    def test_non_stacked_nc_group_is_rejected(self):
        self.nc_pair_fixture(stacked=False)
        report = self.check()
        self.assertTrue(report.blocking)
        self.assertIn("not a proven stacked-pin group", report.issues[0].message)

    def test_proven_same_symbol_stacked_nc_group_is_supported(self):
        self.nc_pair_fixture(stacked=True)
        report = self.check()
        self.assertTrue(report.passed, report.to_json())
        self.assertEqual(report.metadata["no_connects"][0]["kind"], "stacked")

    def test_mutated_bound_inventory_or_nc_set_does_not_match_pinned_capture(self):
        binding = self.bound.components["board.r1"]
        binding.symbol_inventory.pins[0].x_mm += 1
        self.assertTrue(self.check().blocking)
        binding.symbol_inventory.pins[0].x_mm -= 1
        binding.no_connect_pins = ()
        self.assertTrue(self.check().blocking)

    def test_locally_inherited_symbol_with_nc_matches_the_same_resolved_pinned_source(self):
        source = self.fixture / "libraries/Device.kicad_sym"
        tree = parse_quoted(source.read_text())
        tree.append(["symbol", Quoted("Derived"), ["extends", Quoted("R")],
                     ["property", Quoted("Value"), Quoted("Derived"), ["at", "0", "0", "0"]]])
        source.write_text(dumps(tree))
        registry = json.loads((self.fixture / "registry.json").read_text())
        record = next(item for item in registry["libraries"] if item["name"] == "symbol-device")
        record["sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
        (self.fixture / "registry.json").write_text(json.dumps(registry))
        data = json.loads((self.fixture / "bindings.json").read_text())
        data["components"]["board.r1"].update(symbol_name="Derived", symbol_id="Custom:Renamed")
        (self.fixture / "bindings.json").write_text(json.dumps(data))
        self.bound = load_bindings(self.design, self.fixture / "bindings.json")
        self.paths = generate_bound(self.bound, self.root / "inherited-output")
        self.xml = ET.fromstring(self.paths["netlist"].read_text())
        for pin in self.xml.findall("libparts/libpart/pins/pin"):
            if pin.get("name") == "~":
                pin.set("name", "")
        for node in self.xml.findall("nets/net/node"):
            if node.get("pinfunction") == "~":
                del node.attrib["pinfunction"]
        net = ET.SubElement(self.xml.find("nets"), "net", code="99", name="unconnected-inherited-NC")
        ET.SubElement(net, "node", ref="R1", pin="2", pintype="passive+no_connect")
        self.board = parse_quoted(self.paths["board"].read_text())
        binding = self.bound.components["board.r1"]
        self.assertEqual(binding.symbol_inventory.extends, "R")
        self.assertTrue(binding.symbol_inventory.inheritance_resolved)
        self.assertFalse(children(binding.symbol_tree, "extends"))
        report = self.check()
        self.assertTrue(report.passed, report.to_json())
        binding.symbol_inventory.pins[0].electrical_type = "input"
        self.assertTrue(self.check().blocking)

    def test_symbol_name_type_and_libsource_mutations_are_rejected(self):
        pin = self.xml.find("libparts/libpart[@lib='Device']/pins/pin")
        pin.set("type", "input")
        self.assertTrue(self.check().blocking)
        pin.set("type", "passive")
        pin.set("name", "invented")
        self.assertTrue(self.check().blocking)
        pin.set("name", "")
        self.xml.find("components/comp[@ref='R1']/libsource").set("part", "invented")
        self.assertTrue(self.check().blocking)

    def test_duplicate_unknown_nodes_and_false_nc_functions_are_rejected(self):
        self.nc_node.set("pinfunction", "invented")
        self.assertTrue(self.check().blocking)
        self.nc_node.attrib.pop("pinfunction")
        net = self.xml.find("nets/net[@code='99']")
        net.append(deepcopy(self.nc_node))
        self.assertTrue(self.check().blocking)
        net.remove(list(net)[-1])
        self.nc_node.set("pin", "777")
        self.assertTrue(self.check().blocking)

    def test_unsafe_inputs_fail_closed_and_do_not_claim_completion(self):
        self.check()
        self.paths["netlist"].write_text("<!DOCTYPE export><export/>")
        report = check_bound_parity(self.bound, self.paths["netlist"], self.paths["board"])
        self.assertTrue(report.blocking)
        self.assertFalse(report.metadata["completed"])
        self.paths["netlist"].unlink()
        self.paths["netlist"].symlink_to(self.paths["board"])
        self.assertTrue(check_bound_parity(self.bound, self.paths["netlist"], self.paths["board"]).blocking)

    def test_native_smoke_fails_without_the_real_engine_and_never_executes_checks(self):
        from pcb_dev.backends.smoke_no_connect import run_smoke

        destination = self.root / "unavailable-native"
        with patch("pcb_dev.backends.smoke_no_connect.KiCadBackend") as backend:
            backend.return_value.diagnose.return_value = {"supported": False, "available": False}
            result = run_smoke(destination, self.fixture)
            backend.return_value.export_netlist.assert_not_called()
            backend.return_value.check.assert_not_called()
        self.assertEqual(result["status"], "failed")
        self.assertIn("Real KiCad", result["reason"])
        self.assertFalse(result["fabrication_ready"])
        self.assertEqual(json.loads((destination / "smoke-report.json").read_text())["status"], "failed")


if __name__ == "__main__":
    unittest.main()
