import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pcb_dev.libraries import (
    LibraryError, LibraryPin, LibraryRegistry, check_footprint, check_symbol_footprint,
    load_footprint, load_pinned_library, load_registry, load_symbol_library, register_library,
)


FOOTPRINT = '''(footprint "Demo_2Pin"
  (version 20240108) (generator "pcbnew") (layer "F.Cu")
  (fp_line (start -2 -2) (end 2 -2) (stroke (width 0.12) (type default)) (layer "F.SilkS"))
  (fp_rect (start -3 -3) (end 3 3) (stroke (width 0.05) (type default)) (fill none) (layer "F.CrtYd"))
  (pad "1" smd roundrect (at -1 0) (size 1 1.5) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25))
  (pad "2" thru_hole oval (at 1 0 90) (size 1.5 2) (drill oval 0.7 1 (offset 0.1 0)) (layers "*.Cu" "*.Mask"))
  (model "${KICAD10_3DMODEL_DIR}/Demo.step" (offset (xyz 0 0 0)) (scale (xyz 1 1 1)) (rotate (xyz 0 0 0)))
)'''

SYMBOLS = '''(kicad_symbol_lib (version 20231120) (generator "kicad_symbol_editor")
  (symbol "Demo" (property "Reference" "U") (property "Value" "Demo")
    (symbol "Demo_0_1" (rectangle (start -2 -2) (end 2 2)))
    (symbol "Demo_1_1"
      (pin power_in line (at -5 0 0) (length 2.54) (name "VCC" (effects (font (size 1 1)))) (number "1" (effects (font (size 1 1)))))
      (pin passive line (at 5 0 180) (length 2.54) (name "GND") (number "2") hide)))
  (symbol "Derived" (extends "Demo") (property "Value" "Derived"))
)'''


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.footprint = self.root / "Demo.kicad_mod"
        self.symbols = self.root / "Demo.kicad_sym"
        self.footprint.write_text(FOOTPRINT)
        self.symbols.write_text(SYMBOLS)

    def tearDown(self):
        self.temp.cleanup()

    def codes(self, report):
        return {issue.code for issue in report.issues}

    def test_footprint_inventory_reads_real_pad_geometry_and_inert_model_path(self):
        inventory = load_footprint(self.footprint)
        self.assertEqual(inventory.name, "Demo_2Pin")
        self.assertEqual(inventory.version, "20240108")
        self.assertEqual(len(inventory.pads), 2)
        self.assertEqual(inventory.pads[0].layers, ["F.Cu", "F.Paste", "F.Mask"])
        self.assertEqual(inventory.pads[1].rotation, 90)
        self.assertEqual(inventory.pads[1].drill_y_mm, 1)
        self.assertEqual(inventory.pads[1].drill_offset_x_mm, .1)
        self.assertTrue(inventory.courtyard_present)
        self.assertEqual(inventory.courtyard_layers, ["F.CrtYd"])
        self.assertEqual(len(inventory.silk_segments), 1)
        self.assertIn("${KICAD10_3DMODEL_DIR}", inventory.models[0])
        self.assertEqual(len(inventory.sha256), 64)
        self.assertEqual(inventory.to_dict()["pads"][0]["number"], "1")

    def test_valid_geometry_review_is_explicitly_not_manufacturing_qualification(self):
        report = check_footprint(load_footprint(self.footprint))
        self.assertTrue(report.passed)
        self.assertFalse(report.metadata["manufacturing_qualified"])
        self.assertFalse(report.metadata["native"])
        self.assertEqual(report.metadata["courtyard_scope"], "geometry-presence-only")

    def test_positive_pad_size_and_drill_margin_checks(self):
        for replacement, expected in (("(size 0 1.5)", "footprint.pad_size"), ("(size -1 1.5)", "footprint.pad_size")):
            self.footprint.write_text(FOOTPRINT.replace("(size 1 1.5)", replacement))
            self.assertIn(expected, self.codes(check_footprint(load_footprint(self.footprint))))
        self.footprint.write_text(FOOTPRINT.replace("(drill oval 0.7 1 (offset 0.1 0))", "(drill oval 2 2)"))
        self.assertIn("footprint.drill_exceeds_pad", self.codes(check_footprint(load_footprint(self.footprint))))
        self.footprint.write_text(FOOTPRINT.replace("(drill oval 0.7 1 (offset 0.1 0))", ""))
        self.assertIn("footprint.drill_size", self.codes(check_footprint(load_footprint(self.footprint))))

    def test_pin1_and_courtyard_presence_are_review_warnings(self):
        self.footprint.write_text(FOOTPRINT.replace('(pad "1"', '(pad "3"').replace('"F.CrtYd"', '"F.Fab"'))
        codes = self.codes(check_footprint(load_footprint(self.footprint)))
        self.assertIn("footprint.pin1_missing", codes)
        self.assertIn("footprint.courtyard_missing", codes)

    def test_silk_pad_overlap_uses_width_and_matching_copper_side(self):
        self.footprint.write_text(FOOTPRINT.replace("(start -2 -2) (end 2 -2)", "(start -2 0) (end -0.6 0)"))
        codes = self.codes(check_footprint(load_footprint(self.footprint)))
        self.assertIn("footprint.silk_pad_overlap", codes)
        self.footprint.write_text(self.footprint.read_text().replace('"F.SilkS"', '"B.SilkS"'))
        codes = self.codes(check_footprint(load_footprint(self.footprint)))
        self.assertNotIn("footprint.silk_pad_overlap", codes)

    def test_rotated_pad_overlap_matches_kicad_clockwise_coordinate_transform(self):
        text = '''(footprint "Rotated"
          (pad "1" smd rect (at 0 0 45) (size 4 0.2) (layers "F.Cu" "F.Mask"))
          (fp_line (start 1 -1) (end 1.1 -1.1) (stroke (width 0.01) (type default)) (layer "F.SilkS")))'''
        self.footprint.write_text(text)
        self.assertIn("footprint.silk_pad_overlap", self.codes(check_footprint(load_footprint(self.footprint))))
        self.footprint.write_text(text.replace("1 -1", "1 1").replace("1.1 -1.1", "1.1 1.1"))
        self.assertNotIn("footprint.silk_pad_overlap", self.codes(check_footprint(load_footprint(self.footprint))))

    def test_silk_rectangles_polygons_and_unsupported_geometry_are_visible(self):
        self.footprint.write_text('''(footprint "Shapes"
            (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu"))
            (fp_rect (start -1 -1) (end 1 1) (stroke (width 0.1)) (layer "F.SilkS"))
            (fp_poly (pts (xy -1 0) (xy 1 0) (xy 0 1)) (stroke (width 0.1)) (fill solid) (layer "F.SilkS"))
            (fp_circle (center 0 0) (end 2 0) (stroke (width 0.1)) (layer "F.SilkS")))''')
        inventory = load_footprint(self.footprint)
        self.assertEqual(len(inventory.silk_segments), 7)
        self.assertIn("filled fp_poly interior", inventory.unverified_silk_items)
        self.assertIn("fp_circle", inventory.unverified_silk_items)
        self.assertIn("footprint.silk_geometry_unverified", self.codes(check_footprint(inventory)))

    def test_custom_pads_do_not_claim_overlap_coverage(self):
        self.footprint.write_text(FOOTPRINT.replace("smd roundrect", "smd custom"))
        self.assertIn("footprint.pad_geometry_unverified", self.codes(check_footprint(load_footprint(self.footprint))))

    def test_filled_silk_rectangle_interior_is_not_silently_ignored(self):
        self.footprint.write_text(FOOTPRINT.replace('"F.CrtYd"', '"F.SilkS"').replace("(fill none)", "(fill solid)"))
        inventory = load_footprint(self.footprint)
        self.assertIn("filled fp_rect interior", inventory.unverified_silk_items)
        self.assertIn("footprint.silk_geometry_unverified", self.codes(check_footprint(inventory)))

    def test_symbol_inventory_preserves_pin_names_types_units_and_inheritance(self):
        inventory = load_symbol_library(self.symbols)
        self.assertEqual(inventory.version, "20231120")
        base, derived = inventory.symbols
        self.assertEqual(base.pins[0].number, "1")
        self.assertEqual(base.pins[0].name, "VCC")
        self.assertEqual(base.pins[0].electrical_type, "power_in")
        self.assertEqual(base.pins[0].unit, 1)
        self.assertEqual(base.pins[0].convert, 1)
        self.assertTrue(base.pins[1].hidden)
        self.assertEqual(derived.extends, "Demo")
        self.assertTrue(derived.inheritance_resolved)
        self.assertEqual(derived.properties["Reference"], "U")
        self.assertEqual(derived.properties["Value"], "Derived")
        derived.pins[0].name = "edited"
        self.assertEqual(base.pins[0].name, "VCC")
        self.assertEqual(inventory.to_dict()["symbols"][1]["extends"], "Demo")

    def test_symbol_pin_hide_yes_no_form(self):
        self.symbols.write_text(SYMBOLS.replace(' hide)))', ' (hide no))))'))
        self.assertFalse(load_symbol_library(self.symbols).symbols[0].pins[1].hidden)

    def test_symbol_inheritance_cycles_missing_parents_and_duplicate_properties_reject(self):
        texts = [
            '(kicad_symbol_lib (symbol "A" (extends "B")) (symbol "B" (extends "A")))',
            '(kicad_symbol_lib (symbol "A" (extends "Missing")))',
            '(kicad_symbol_lib (symbol "A" (property "Value" "A") (property "Value" "B")))',
            '(kicad_symbol_lib (symbol "A") (symbol "A"))',
            '(kicad_symbol_lib (symbol "A" (symbol "bad_name")))',
        ]
        for text in texts:
            self.symbols.write_text(text)
            with self.subTest(text=text), self.assertRaises(LibraryError):
                load_symbol_library(self.symbols)

    def test_symbol_footprint_pin_mapping_requires_exact_number_coverage(self):
        footprint = load_footprint(self.footprint)
        symbol = load_symbol_library(self.symbols).symbols[0]
        self.assertTrue(check_symbol_footprint(symbol, footprint).passed)
        symbol.pins[0].number = "3"
        report = check_symbol_footprint(symbol, footprint)
        self.assertIn("library.pin_without_pad", self.codes(report))
        self.assertIn("library.pad_without_pin", self.codes(report))
        self.assertFalse(report.metadata["datasheet_pinout_verified"])

    def test_nonfinite_duplicate_and_malformed_pad_fields_are_rejected(self):
        variants = [
            FOOTPRINT.replace("(size 1 1.5)", "(size nan 1.5)"),
            FOOTPRINT.replace("(size 1 1.5)", "(size 1 1.5) (size 2 2)"),
            FOOTPRINT.replace('(layers "F.Cu" "F.Paste" "F.Mask")', '(layers "F.Cu" "F.Cu")'),
            FOOTPRINT.replace("(drill oval 0.7 1 (offset 0.1 0))", "(drill oval 1)"),
            FOOTPRINT.replace("(at -1 0)", "(at 1e999 0)"),
            FOOTPRINT[:-1], '(module "legacy")',
        ]
        for text in variants:
            self.footprint.write_text(text)
            with self.subTest(text=text[:60]), self.assertRaises(LibraryError):
                load_footprint(self.footprint)

    def test_malicious_strings_remain_data_and_external_models_are_never_opened(self):
        self.footprint.write_text(FOOTPRINT.replace('"Demo_2Pin"', '"__import__(os).system(evil)"'))
        self.assertEqual(load_footprint(self.footprint).name, "__import__(os).system(evil)")
        self.footprint.write_text(FOOTPRINT.replace("${KICAD10_3DMODEL_DIR}/Demo.step", "https://invalid.example/model.step"))
        inventory = load_footprint(self.footprint)
        self.assertEqual(inventory.models, ["https://invalid.example/model.step"])

    def test_file_size_depth_and_syntax_are_bounded(self):
        with patch("pcb_dev.libraries.MAX_LIBRARY_BYTES", 10), self.assertRaises(LibraryError):
            load_footprint(self.footprint)
        self.footprint.write_text("(" * 130 + ")" * 130)
        with self.assertRaises(LibraryError):
            load_footprint(self.footprint)
        self.footprint.write_bytes(b"\xff\xfe")
        with self.assertRaises(LibraryError):
            load_footprint(self.footprint)

    def test_registry_pins_actual_file_bytes_and_rejects_later_edits(self):
        registry = self.root / "libraries.lock.json"
        pin = register_library(self.footprint, registry, name="demo-footprint")
        self.assertEqual(pin.kind, "footprint")
        self.assertEqual(pin.file, "Demo.kicad_mod")
        self.assertEqual(load_pinned_library(registry, "demo-footprint").name, "Demo_2Pin")
        self.footprint.write_text(FOOTPRINT + "\n")
        with self.assertRaisesRegex(LibraryError, "differs from its SHA-256"):
            load_pinned_library(registry, "demo-footprint")
        register_library(self.footprint, registry, name="demo-footprint")
        self.assertEqual(len(load_registry(registry).libraries), 1)
        register_library(self.symbols, registry, name="demo-symbols")
        self.assertEqual(load_pinned_library(registry, "demo-symbols").symbols[0].name, "Demo")

    def test_registry_kind_is_checked_against_content(self):
        with self.assertRaisesRegex(LibraryError, "expected a modern KiCad symbol"):
            register_library(self.footprint, self.root / "lock.json", name="bad", kind="symbols")
        registry = self.root / "lock.json"
        register_library(self.footprint, registry, name="demo")
        with self.assertRaisesRegex(LibraryError, "not registered"):
            load_pinned_library(registry, "missing")

    def test_registry_traversal_and_symlink_escape_are_rejected(self):
        for path in ("../outside", "/tmp/outside", "a\\b", "C:/a", "a//b", "./a"):
            with self.subTest(path=path), self.assertRaises(LibraryError):
                LibraryPin.from_dict({"name": "part", "kind": "footprint", "file": path, "sha256": "0" * 64})
        with tempfile.TemporaryDirectory() as external:
            external_file = Path(external) / "outside.kicad_mod"
            external_file.write_text(FOOTPRINT)
            with self.assertRaisesRegex(LibraryError, "inside the registry"):
                register_library(external_file, self.root / "lock.json", name="bad")
            registry = self.root / "lock.json"
            pin = register_library(self.footprint, registry, name="demo")
            self.footprint.unlink()
            self.footprint.symlink_to(external_file)
            with self.assertRaisesRegex(LibraryError, "symlink escapes"):
                load_pinned_library(registry, pin.name)

    def test_duplicate_alias_unknown_fields_and_invalid_schema_are_rejected(self):
        pin = LibraryPin("part", "footprint", "part.kicad_mod", "0" * 64)
        invalid = [
            LibraryRegistry([pin, copy.deepcopy(pin)]).to_dict(),
            {"schema_version": True, "libraries": []},
            {"schema_version": 1, "libraries": [], "hooks": []},
        ]
        for data in invalid:
            with self.subTest(data=data), self.assertRaises(LibraryError):
                LibraryRegistry.from_dict(data)
        registry = self.root / "lock.json"
        registry.write_text('{"schema_version": 1, "schema_version": 1, "libraries": []}')
        with self.assertRaises(LibraryError):
            load_registry(registry)


if __name__ == "__main__":
    unittest.main()
