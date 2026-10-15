import tempfile
import unittest
from pathlib import Path

from pcb_dev.model import Design
from pcb_dev.parity import ParityError, check_parity, parse_board, parse_netlist, parse_sexpr


MODEL = {"schema_version": 1, "name": "parity", "version": "0.1.0",
         "board": {"width_mm": 20, "height_mm": 20}, "root": {"id": "board", "components": [
             {"id": "resistor", "reference": "R1", "value": "1k", "footprint": "Resistor:R_0603",
              "pins": {"1": "A", "2": "GND"}}]}}
PCB = '''(kicad_pcb (version 20241229) (net 1 "A") (net 2 "GND")
 (footprint "Resistor:R_0603" (property "Reference" "R1") (property "Value" "1k")
  (pad "1" smd rect (net 1 "A")) (pad "2" smd rect (net 2 "GND"))))'''
NETLIST = '''<export><components><comp ref="R1"><value>1k</value>
<footprint>Resistor:R_0603</footprint></comp></components><nets>
<net code="1" name="A"><node ref="R1" pin="1"/></net>
<net code="2" name="GND"><node ref="R1" pin="2"/></net></nets></export>'''


class ParityTests(unittest.TestCase):
    def check(self, pcb=PCB, netlist=NETLIST):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            (base / "board.kicad_pcb").write_text(pcb)
            (base / "schematic.xml").write_text(netlist)
            return check_parity(Design.from_dict(MODEL), base / "schematic.xml", base / "board.kicad_pcb")

    def test_exact_mapping_passes_without_claiming_footprint_qualification(self):
        report = self.check()
        self.assertFalse(report.blocking)
        self.assertTrue(report.metadata["completed"])
        self.assertFalse(report.metadata["physical_footprints_qualified"])

    def test_reference_value_footprint_and_pin_net_drift_block(self):
        for before, after in [("R1", "R2"), ("1k", "2k"), ("R_0603", "R_0805"), ('"A"', '"B"')]:
            with self.subTest(before=before):
                self.assertTrue(self.check(pcb=PCB.replace(before, after)).blocking)
        self.assertTrue(self.check(netlist=NETLIST.replace('name="A"', 'name="B"')).blocking)

    def test_preview_marker_blocks_even_if_mapping_matches(self):
        report = self.check(pcb=PCB.replace("(version", '(gr_text "PCBDEV_PREVIEW_ONLY") (version'))
        self.assertIn("parity.preview_only", [issue.code for issue in report.issues])

    def test_pad_net_codes_must_match_declared_net_table(self):
        for content in [PCB.replace('(net 1 "A")', '(net 1 "B")', 1),
                        PCB.replace('(net 1 "A")', '(net 1 "A") (net 1 "GND")', 1),
                        PCB.replace('(net 1 "A")', '', 1),
                        PCB.replace('(property "Value" "1k")', '(property "Value" "1k") (fp_text value "2k")')]:
            with self.subTest(content=content):
                self.assertTrue(self.check(pcb=content).blocking)
        self.assertTrue(self.check(netlist=NETLIST.replace('<value>1k</value>',
                           '<value>1k</value><value>2k</value>')).blocking)

    def test_untrusted_xml_entity_duplicate_node_and_unknown_ref_block(self):
        for content in ['<!DOCTYPE export><export/>', NETLIST.replace('pin="2"', 'pin="1"'),
                        NETLIST.replace('ref="R1" pin', 'ref="R2" pin'), '<not-kicad/>']:
            with self.subTest(content=content):
                self.assertTrue(self.check(netlist=content).blocking)

    def test_parse_failures_leave_incomplete_evidence(self):
        report = self.check(pcb="(kicad_pcb")
        self.assertTrue(report.blocking)
        self.assertFalse(report.metadata["completed"])

    def test_data_parser_escapes_and_resource_limits(self):
        self.assertEqual(parse_sexpr('(a "quoted \\"value\\"" (b c))'),
                         ["a", 'quoted "value"', ["b", "c"]])
        for invalid in ["(a)(b)", "a", "(a", "(a))", '"a"', "(" * 129 + ")" * 129]:
            with self.subTest(invalid=invalid), self.assertRaises(ParityError):
                parse_sexpr(invalid)

    def test_repeated_physical_pad_same_net_is_supported(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "board.kicad_pcb"
            path.write_text(PCB.replace('(pad "2"', '(pad "1" smd rect (net 1 "A")) (pad "2"'))
            self.assertEqual(parse_board(path)["R1"]["pins"], {"1": "A", "2": "GND"})
            path.write_text(PCB.replace('(pad "2"', '(pad "1" smd rect (net 3 "B")) (pad "2"'))
            with self.assertRaises(ParityError):
                parse_board(path)

    def test_duplicate_component_net_names_and_missing_input_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "netlist.xml"
            path.write_text(NETLIST.replace('name="GND"', 'name="A"'))
            with self.assertRaises(ParityError):
                parse_netlist(path)
            with self.assertRaises(ParityError):
                parse_netlist(path.with_name("missing.xml"))
