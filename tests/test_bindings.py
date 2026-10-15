import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pcb_dev.bindings import BindingsError, load_bindings
from pcb_dev.model import Design
from pcb_dev.sexpr import Quoted, dumps, parse_quoted


SYMBOLS = '''(kicad_symbol_lib (version 20231120) (generator "pcb-dev-test")
  (symbol "Base" (pin_names (offset 0)) (in_bom yes) (on_board yes)
    (property "Reference" "R" (at 0 0 0) (effects (font (size 1.27 1.27))))
    (property "Value" "Base" (at 0 0 0) (effects (font (size 1.27 1.27))))
    (symbol "Base_0_1" (rectangle (start -1 -1) (end 1 1) (stroke (width 0.254) (type default)) (fill (type none))))
    (symbol "Base_1_1"
      (pin passive line (at 0 2.54 270) (length 1.27) (name "A") (number "1"))
      (pin passive line (at 0 -2.54 90) (length 1.27) (name "B") (number "2"))))
  (symbol "Derived" (extends "Base") (property "Value" "Derived" (at 0 0 0)))
)'''

FOOTPRINT = '''(footprint "R_Test" (version 20240108) (generator "pcb-dev-test") (layer "F.Cu")
  (fp_rect (start -2 -2) (end 2 2) (stroke (width 0.05) (type default)) (fill none) (layer "F.CrtYd"))
  (fp_line (start -1 -1) (end 1 -1) (stroke (width 0.12) (type default)) (layer "F.SilkS"))
  (pad "1" smd roundrect (at -1 0) (size 0.8 1.2) (layers "F.Cu" "F.Mask" "F.Paste") (roundrect_rratio 0.25))
  (pad "2" smd roundrect (at 1 0) (size 0.8 1.2) (layers "F.Cu" "F.Mask" "F.Paste") (roundrect_rratio 0.25))
)'''


def model():
    return Design.from_dict({"schema_version": 1, "name": "bound", "version": "0.1.0",
                            "board": {"width_mm": 20, "height_mm": 20}, "root": {
                                "id": "board", "components": [
                                    {"id": "resistor", "reference": "R1", "value": "1k", "footprint": "Test:R_Test",
                                     "pins": {"1": "VCC", "2": "GND"},
                                     "position": {"x_mm": 5, "y_mm": 6, "rotation": 90, "side": "top"}},
                                ]}})


class BindingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "libraries").mkdir()
        self.symbols = self.root / "libraries" / "Test.kicad_sym"
        self.footprint = self.root / "libraries" / "R_Test.kicad_mod"
        # Source provenance is byte exact; avoid platform newline translation.
        self.symbols.write_bytes(SYMBOLS.encode("utf-8"))
        self.footprint.write_bytes(FOOTPRINT.encode("utf-8"))
        self.registry_path = self.root / "registry.json"
        self.bindings_path = self.root / "bindings.json"
        self.registry = {"schema_version": 1, "libraries": [
            {"name": "symbols", "kind": "symbols", "file": "libraries/Test.kicad_sym", "sha256": ""},
            {"name": "footprint", "kind": "footprint", "file": "libraries/R_Test.kicad_mod", "sha256": ""},
        ]}
        self.entry = {"symbol_library": "symbols", "symbol_name": "Base", "symbol_id": "Device:R",
                      "footprint_library": "footprint", "unit": 1, "no_connect_pins": []}
        self.bindings = {"schema_version": 1, "registry": "registry.json", "components": {"board.resistor": self.entry}}
        self.pin_sources()
        self.write_bindings()

    def tearDown(self):
        self.temp.cleanup()

    def pin_sources(self):
        self.registry["libraries"][0]["sha256"] = hashlib.sha256(self.symbols.read_bytes()).hexdigest()
        self.registry["libraries"][1]["sha256"] = hashlib.sha256(self.footprint.read_bytes()).hexdigest()
        self.registry_path.write_text(json.dumps(self.registry, indent=2) + "\n")

    def write_bindings(self):
        self.bindings_path.write_text(json.dumps(self.bindings, indent=2) + "\n")

    def load(self, design=None):
        return load_bindings(model() if design is None else design, self.bindings_path)

    def test_complete_binding_retains_real_source_and_model_fields(self):
        original = model()
        bound = self.load(original)
        self.assertEqual(set(bound.components), {"board.resistor"})
        component = bound.components["board.resistor"]
        self.assertEqual(component.symbol_id, "Device:R")
        self.assertEqual(component.footprint_id, "Test:R_Test")
        self.assertEqual(component.component.position.rotation, 90)
        self.assertEqual(component.symbol_inventory.pins[0].electrical_type, "passive")
        self.assertEqual(component.footprint_inventory.pads[0].shape, "roundrect")
        self.assertEqual(component.footprint_inventory.pads[0].size_x_mm, .8)
        self.assertEqual(component.symbol_tree[1], "Device:R")
        self.assertIs(type(component.symbol_tree[1]), Quoted)
        units = [node[1] for node in component.symbol_tree[2:] if node[0] == "symbol"]
        self.assertEqual(units, ["R_0_1", "R_1_1"])
        self.assertEqual(component.footprint_tree, parse_quoted(FOOTPRINT))
        self.assertEqual(bound.design.fingerprint(), original.fingerprint())

    def test_quote_classes_are_preserved_in_source_and_embedded_definition(self):
        component = self.load().components["board.resistor"]
        self.assertIs(type(component.symbol_tree[0]), str)
        self.assertIs(type(component.symbol_tree[1]), Quoted)
        unit = next(node for node in component.symbol_tree[2:] if node[0] == "symbol" and str(node[1]).endswith("_1_1"))
        self.assertIs(type(unit[2][1]), str)
        self.assertEqual(unit[2][1], "passive")
        self.assertIs(type(next(node for node in unit[2][3:] if node[0] == "number")[1]), Quoted)
        self.assertIn('(number "1")', dumps(component.symbol_tree))
        self.assertIn("(in_bom yes)", dumps(component.symbol_tree))

    def test_provenance_and_captured_bytes_are_exact_without_absolute_paths(self):
        bound = self.load()
        self.assertEqual(bound.bindings_content, self.bindings_path.read_bytes())
        self.assertEqual(bound.registry_content, self.registry_path.read_bytes())
        self.assertEqual(bound.source_contents["symbols"], self.symbols.read_bytes())
        self.assertEqual(bound.provenance["bindings_sha256"], hashlib.sha256(bound.bindings_content).hexdigest())
        self.assertEqual(bound.provenance["registry_sha256"], hashlib.sha256(bound.registry_content).hexdigest())
        self.assertEqual(bound.provenance["registry"], "registry.json")
        self.assertNotIn(str(self.root), json.dumps(bound.provenance))
        self.symbols.write_text("changed after capture")
        self.assertEqual(bound.source_contents["symbols"], SYMBOLS.encode("utf-8"))

    def test_bound_model_and_per_component_trees_do_not_alias_input(self):
        original = model()
        duplicate = copy.deepcopy(original.root.components[0])
        duplicate.id, duplicate.reference = "second", "R2"
        original.root.components.append(duplicate)
        self.bindings["components"]["board.second"] = copy.deepcopy(self.entry)
        self.write_bindings()
        bound = self.load(original)
        bound.components["board.resistor"].symbol_tree[1] = Quoted("Changed:X")
        self.assertEqual(bound.components["board.second"].symbol_tree[1], "Device:R")
        bound.components["board.resistor"].component.value = "changed"
        self.assertEqual(original.root.components[0].value, "1k")

    def test_local_inheritance_flattens_properties_flags_graphics_and_pins(self):
        self.entry.update(symbol_name="Derived", symbol_id="Custom:Renamed")
        self.write_bindings()
        component = self.load().components["board.resistor"]
        tree = component.symbol_tree
        self.assertFalse(any(node[0] == "extends" for node in tree[2:]))
        properties = {node[1]: node[2] for node in tree[2:] if node[0] == "property"}
        self.assertEqual(properties, {"Reference": "R", "Value": "Derived"})
        self.assertTrue(any(node[0] == "pin_names" for node in tree[2:]))
        self.assertEqual([node[1] for node in tree[2:] if node[0] == "symbol"], ["Renamed_0_1", "Renamed_1_1"])
        self.assertEqual(len(component.symbol_inventory.pins), 2)

    def test_every_component_including_dnp_must_have_exactly_one_binding(self):
        for mutation in ("missing", "extra", "dnp"):
            self.bindings["components"] = {"board.resistor": copy.deepcopy(self.entry)}
            design = model()
            if mutation == "extra":
                self.bindings["components"]["board.unknown"] = copy.deepcopy(self.entry)
            else:
                self.bindings["components"].clear()
                if mutation == "dnp":
                    design.root.components[0].fitted = False
            self.write_bindings()
            with self.subTest(mutation=mutation), self.assertRaisesRegex(BindingsError, "exactly match"):
                self.load(design)

    def test_pin_maps_require_explicit_exhaustive_connected_or_no_connect_sets(self):
        design = model()
        del design.root.components[0].pins["2"]
        with self.assertRaisesRegex(BindingsError, "incomplete symbol pin map"):
            self.load(design)
        self.entry["no_connect_pins"] = ["2"]
        self.write_bindings()
        self.assertEqual(self.load(design).components["board.resistor"].no_connect_pins, ("2",))
        with self.assertRaisesRegex(BindingsError, "connected and explicitly no-connect"):
            self.load()
        self.entry["no_connect_pins"] = ["3"]
        self.write_bindings()
        with self.assertRaisesRegex(BindingsError, "unknown=.*3"):
            self.load(design)

    def test_invalid_no_connect_arrays_and_supplied_unit_types_are_rejected(self):
        original = copy.deepcopy(self.entry)
        for value in (None, "2", [2], [""], ["2", "2"]):
            self.entry.clear()
            self.entry.update(copy.deepcopy(original))
            self.entry["no_connect_pins"] = value
            self.write_bindings()
            with self.subTest(no_connect=value), self.assertRaises(BindingsError):
                self.load()
        for value in (True, 1.0, "1", 0, 2, None):
            self.entry.clear()
            self.entry.update(copy.deepcopy(original))
            self.entry["unit"] = value
            self.write_bindings()
            with self.subTest(unit=value), self.assertRaisesRegex(BindingsError, "unit 1"):
                self.load()

    def test_mismatched_pad_numbers_and_invalid_geometry_fail_before_output(self):
        for text in (FOOTPRINT.replace('(pad "2"', '(pad "3"'), FOOTPRINT.replace("(size 0.8 1.2)", "(size 0 1.2)")):
            self.footprint.write_text(text)
            self.pin_sources()
            with self.subTest(text=text[:40]), self.assertRaises(BindingsError):
                self.load()
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), ["bindings.json", "libraries", "registry.json"])

    def test_multiunit_and_alternate_conversion_even_graphics_only_are_rejected(self):
        for suffix in ("2_1", "1_2", "0_2"):
            self.symbols.write_text(SYMBOLS.replace('Base_0_1', 'Base_' + suffix))
            self.pin_sources()
            with self.subTest(suffix=suffix), self.assertRaisesRegex(BindingsError, "multi-unit|conversion"):
                self.load()

    def test_bottom_placement_and_pretransformed_source_footprints_are_rejected(self):
        design = model()
        design.root.components[0].position.side = "bottom"
        with self.assertRaisesRegex(BindingsError, "top-side"):
            self.load(design)
        for before, after in (('(layer "F.Cu")', '(layer "B.Cu")'),
                              ("(version 20240108)", "(version 20240108) (at 0 0 90)"),
                              ("(version 20240108)", "(version 20240108) (at 1 0)")):
            self.footprint.write_text(FOOTPRINT.replace(before, after, 1))
            self.pin_sources()
            with self.subTest(after=after), self.assertRaises(BindingsError):
                self.load()

    def test_missing_physical_position_remains_an_explicit_draft(self):
        design = model()
        design.root.components[0].position = None
        self.assertIsNone(self.load(design).components["board.resistor"].component.position)

    def test_identifier_source_name_and_alias_kind_must_match(self):
        for changes in ({"symbol_id": "not-a-lib-id"}, {"symbol_id": "../bad:R"},
                        {"symbol_name": "Missing"}, {"symbol_library": "footprint"},
                        {"footprint_library": "missing"}):
            self.entry.update(changes)
            self.write_bindings()
            with self.subTest(changes=changes), self.assertRaises(BindingsError):
                self.load()
            self.entry.update(symbol_library="symbols", symbol_name="Base", symbol_id="Device:R", footprint_library="footprint")
        self.write_bindings()
        design = model()
        design.root.components[0].footprint = "Test:Wrong"
        with self.assertRaisesRegex(BindingsError, "source name"):
            self.load(design)

    def test_source_hash_changes_are_never_repaired_or_fallen_back_automatically(self):
        self.symbols.write_text(SYMBOLS + "\n")
        before = self.registry_path.read_bytes()
        with self.assertRaisesRegex(BindingsError, "SHA-256"):
            self.load()
        self.assertEqual(self.registry_path.read_bytes(), before)

    def test_binding_and_registry_unknown_missing_duplicate_or_nonfinite_fields_reject(self):
        variants = [{**copy.deepcopy(self.bindings), "extra": 1},
                    {**copy.deepcopy(self.bindings), "schema_version": True},
                    {"schema_version": 1, "components": {}}]
        for data in variants:
            self.bindings_path.write_text(json.dumps(data))
            with self.subTest(data=data), self.assertRaises(BindingsError):
                self.load()
        self.entry["extra"] = 1
        self.write_bindings()
        with self.assertRaises(BindingsError):
            self.load()
        del self.entry["extra"]
        self.write_bindings()
        self.bindings_path.write_text('{"schema_version":1,"schema_version":1}')
        with self.assertRaisesRegex(BindingsError, "duplicate"):
            self.load()
        self.write_bindings()
        self.registry_path.write_text('{"schema_version":1,"libraries":[],"bad":NaN}')
        with self.assertRaises(BindingsError):
            self.load()

    def test_containment_rejects_traversal_absolute_windows_and_symlink_escapes(self):
        for value in ("../registry.json", str(self.registry_path), "a\\b", "C:/registry.json", "./registry.json", "a//b"):
            self.bindings["registry"] = value
            self.write_bindings()
            with self.subTest(registry=value), self.assertRaises(BindingsError):
                self.load()
        self.bindings["registry"] = "registry.json"
        self.write_bindings()
        with tempfile.TemporaryDirectory() as outside:
            external = Path(outside) / "external.kicad_mod"
            external.write_text(FOOTPRINT)
            self.footprint.unlink()
            self.footprint.symlink_to(external)
            with self.assertRaisesRegex(BindingsError, "symlink escapes"):
                self.load()

    def test_registry_containment_is_independent_for_nested_registry(self):
        nested = self.root / "nested"
        nested.mkdir()
        (nested / "registry.json").write_bytes(self.registry_path.read_bytes())
        self.bindings["registry"] = "nested/registry.json"
        self.write_bindings()
        with self.assertRaisesRegex(BindingsError, "missing or invalid local path"):
            self.load()

    def test_conflicting_symbol_id_definitions_are_rejected(self):
        design = model()
        second = copy.deepcopy(design.root.components[0])
        second.id, second.reference = "second", "R2"
        design.root.components.append(second)
        self.bindings["components"]["board.second"] = {**copy.deepcopy(self.entry), "symbol_name": "Derived"}
        self.write_bindings()
        with self.assertRaisesRegex(BindingsError, "conflicting source definitions"):
            self.load(design)

    def test_duplicate_physical_symbol_pins_must_have_identical_meaning_and_position(self):
        pin = '(pin passive line (at 0 2.54 270) (length 1.27) (name "A") (number "1"))'
        self.symbols.write_text(SYMBOLS.replace(pin, pin + pin))
        self.pin_sources()
        self.assertEqual(len(self.load().components["board.resistor"].symbol_inventory.pins), 3)
        self.symbols.write_text(SYMBOLS.replace(pin, pin + pin.replace("0 2.54", "1 2.54")))
        self.pin_sources()
        with self.assertRaisesRegex(BindingsError, "conflicting duplicate"):
            self.load()

    def test_unresolved_cyclic_or_unsupported_inheritance_is_rejected(self):
        variants = [SYMBOLS.replace('(extends "Base")', '(extends "Missing")'),
                    SYMBOLS.replace('(symbol "Base"', '(symbol "Base" (extends "Derived")', 1),
                    SYMBOLS.replace('(extends "Base")', '(extends "Base") (symbol "Derived_0_1" (circle (center 0 0) (radius 1)))')]
        self.entry["symbol_name"] = "Derived"
        self.write_bindings()
        for source in variants:
            self.symbols.write_text(source)
            self.pin_sources()
            with self.subTest(source=source[:40]), self.assertRaises(BindingsError):
                self.load()

    def test_unknown_escapes_and_quoted_tags_are_not_silently_changed(self):
        for source in (SYMBOLS.replace('"Base"', '"Bad\\x"', 1), SYMBOLS.replace("(pin passive", '("pin" passive', 1)):
            self.symbols.write_text(source)
            self.pin_sources()
            with self.subTest(source=source[:40]), self.assertRaises(BindingsError):
                self.load()

    def test_license_attribution_files_are_captured_exactly_with_hashes(self):
        attribution = self.root / "ATTRIBUTION.md"
        attribution.write_text("Source upstream, licence CC-BY-SA-4.0.\n", encoding="utf-8")
        self.bindings["licenses"] = ["ATTRIBUTION.md"]
        self.write_bindings()
        bound = self.load()
        self.assertEqual(bound.license_contents["ATTRIBUTION.md"], attribution.read_bytes())
        self.assertEqual(bound.provenance["licenses"]["ATTRIBUTION.md"]["sha256"], hashlib.sha256(attribution.read_bytes()).hexdigest())
        attribution.write_text("changed")
        self.assertNotEqual(bound.license_contents["ATTRIBUTION.md"], attribution.read_bytes())

    def test_invalid_duplicate_overlapping_or_nontext_license_files_are_rejected(self):
        (self.root / "LICENSE.txt").write_text("license")
        for files in (["../outside"], ["LICENSE.txt", "LICENSE.txt"], ["bindings.json"],
                      ["registry.json"], ["libraries/Test.kicad_sym"], "LICENSE.txt"):
            self.bindings["licenses"] = files
            self.write_bindings()
            with self.subTest(files=files), self.assertRaises(BindingsError):
                self.load()
        (self.root / "LICENSE.txt").write_bytes(b"\xff\x00")
        self.bindings["licenses"] = ["LICENSE.txt"]
        self.write_bindings()
        with self.assertRaisesRegex(BindingsError, "UTF-8 text"):
            self.load()

    def test_size_and_aggregate_limits_are_enforced(self):
        for setting in ("MAX_BINDINGS_BYTES", "MAX_SOURCE_BYTES", "MAX_TOTAL_SOURCE_BYTES"):
            with self.subTest(setting=setting), patch("pcb_dev.bindings." + setting, 20), self.assertRaises(BindingsError):
                self.load()
        (self.root / "LICENSE.txt").write_text("long license")
        self.bindings["licenses"] = ["LICENSE.txt"]
        self.write_bindings()
        with patch("pcb_dev.bindings.MAX_LICENSE_BYTES", 3), self.assertRaises(BindingsError):
            self.load()

    def test_official_pinned_fixture_loads_with_attribution(self):
        fixture = Path(__file__).resolve().parents[1] / "examples" / "library_divider"
        from pcb_dev.model import load_design
        bound = load_bindings(load_design(fixture / "design.json"), fixture / "bindings.json")
        self.assertEqual(set(bound.components), {"board.j1", "board.r1", "board.r2"})
        self.assertEqual(len(bound.source_contents), 4)
        self.assertEqual(len(bound.license_contents), 4)
        self.assertEqual(bound.components["board.r2"].component.position.rotation, 270)


if __name__ == "__main__":
    unittest.main()
