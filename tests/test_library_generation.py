"""Library drafts preserve pinned electrical and physical definitions."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest
import xml.etree.ElementTree as ET

from pcb_dev.backends import LibraryGenerationError, generate_bound
from pcb_dev.bindings import load_bindings
from pcb_dev.model import Design, load_design
from pcb_dev.parity import check_parity, parse_board
from pcb_dev.sexpr import Quoted, parse_quoted


FIXTURE = Path(__file__).resolve().parents[1] / "examples" / "library_divider"


def children(node, key):
    return [item for item in node[1:] if isinstance(item, list) and item and item[0] == key]


def one(node, key):
    fields = children(node, key)
    if len(fields) != 1:
        raise AssertionError(f"expected exactly one {key}")
    return fields[0]


def descendants(node, key):
    found = []
    if isinstance(node, list):
        if node and node[0] == key:
            found.append(node)
        for child in node:
            found.extend(descendants(child, key))
    return found


class LibraryGenerationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.fixture = self.root / "fixture"
        shutil.copytree(FIXTURE, self.fixture)
        self.design = load_design(self.fixture / "design.json")

    def tearDown(self):
        self.temporary.cleanup()

    def bound(self, design=None):
        return load_bindings(design or self.design, self.fixture / "bindings.json")

    def paths(self, bound=None, directory="output"):
        return generate_bound(bound or self.bound(), self.root / directory)

    def test_actual_fixture_generates_model_derived_parity_and_explicit_draft_markers(self):
        paths = self.paths()
        self.assertTrue({"board", "schematic", "netlist", "project", "rules", "manifest", "symbol_table", "footprint_table"} <= paths.keys())
        self.assertTrue(check_parity(self.design, paths["netlist"], paths["board"]).passed)
        parts = parse_board(paths["board"])
        self.assertEqual(parts["J1"]["footprint"], self.design.root.components[0].footprint)
        self.assertEqual(parts["R1"]["pins"], {"1": "VIN", "2": "VOUT"})
        for name in ("board", "schematic"):
            text = paths[name].read_text()
            self.assertIn("PCBDEV_LIBRARY_DRAFT", text)
            self.assertNotIn("PCBDEV_PREVIEW_ONLY", text)
            self.assertNotIn("GenericPreview", text)
        manifest = json.loads(paths["manifest"].read_text())
        for field in ("fabrication_ready", "routed", "qualified"):
            self.assertFalse(manifest[field])
        self.assertEqual(manifest["kind"], "library-draft")
        self.assertEqual(manifest["design_sha256"], self.design.fingerprint())
        self.assertEqual(manifest["provenance"], self.bound().provenance)

    def test_all_artifacts_are_deterministic_and_source_trees_are_not_mutated(self):
        bound = self.bound()
        before = {key: (deepcopy(value.symbol_tree), deepcopy(value.footprint_tree)) for key, value in bound.components.items()}
        first = self.paths(bound, "one")
        second = self.paths(bound, "two")
        self.assertEqual(first.keys(), second.keys())
        for key in first:
            self.assertEqual(first[key].read_bytes(), second[key].read_bytes(), key)
        for key, component in bound.components.items():
            self.assertEqual((component.symbol_tree, component.footprint_tree), before[key])
        manifest = json.loads(first["manifest"].read_text())
        for key, artifact in manifest["artifacts"].items():
            self.assertEqual(artifact["sha256"], hashlib.sha256(first[key].read_bytes()).hexdigest())

    def test_physical_pad_geometry_drills_courtyards_and_angles_are_preserved(self):
        bound = self.bound()
        tree = parse_quoted(self.paths(bound)["board"].read_text())
        placed = {}
        for footprint in children(tree, "footprint"):
            reference = next(field[2] for field in children(footprint, "property") if field[1] == "Reference")
            placed[reference] = footprint
        for binding in bound.components.values():
            footprint = placed[binding.component.reference]
            self.assertEqual(one(footprint, "at")[1:], [str(binding.component.position.x_mm), str(binding.component.position.y_mm), str(binding.component.position.rotation)])
            original_pads = children(binding.footprint_tree, "pad")
            new_pads = children(footprint, "pad")
            self.assertEqual(len(original_pads), len(new_pads))
            for original, pad in zip(original_pads, new_pads):
                self.assertEqual(pad[1:4], original[1:4])
                for field in ("size", "drill", "layers", "roundrect_rratio"):
                    self.assertEqual(children(pad, field), children(original, field), (binding.path, pad[1], field))
                before = one(original, "at")
                after = one(pad, "at")
                # The file stores local pad coordinates/angles. The native
                # geometry smoke separately checks global source+pose angles.
                self.assertEqual(after, before)
            for layer in ("F.CrtYd", "F.SilkS", "F.Fab"):
                old_graphics = [node for node in binding.footprint_tree if isinstance(node, list) and node and node[0].startswith("fp_") and children(node, "layer") and one(node, "layer")[1] == layer]
                new_graphics = [node for node in footprint if isinstance(node, list) and node and node[0].startswith("fp_") and children(node, "layer") and one(node, "layer")[1] == layer]
                self.assertEqual(len(new_graphics), len(old_graphics))

    def test_project_libraries_are_portable_and_exact_definitions_match_cache(self):
        bound = self.bound()
        paths = self.paths(bound)
        schematic = parse_quoted(paths["schematic"].read_text())
        embedded = {node[1]: node for node in children(one(schematic, "lib_symbols"), "symbol")}
        self.assertEqual(set(embedded), {"Device:R", "Connector_Generic:Conn_01x02"})
        for key, path in paths.items():
            if key.startswith("symbol_library_"):
                local = parse_quoted(path.read_text())
                names = {node[1] for node in children(local, "symbol")}
                self.assertNotIn("Device:R", names)
                for node in children(local, "symbol"):
                    matching = [source for identifier, source in embedded.items() if identifier.split(":")[1] == node[1]]
                    self.assertEqual(len(matching), 1)
                    self.assertEqual(node[2:], matching[0][2:])
        for name in ("symbol_table", "footprint_table"):
            table = parse_quoted(paths[name].read_text())
            for library in children(table, "lib"):
                uri = one(library, "uri")[1]
                self.assertTrue(uri.startswith("${KIPRJMOD}/"))
                self.assertTrue((paths["board"].parent / uri.removeprefix("${KIPRJMOD}/")).exists())
                self.assertNotIn(str(self.root), uri)

    def test_source_quote_classes_and_actual_electrical_pin_types_survive(self):
        library = self.fixture / "libraries" / "Device.kicad_sym"
        library.write_text(library.read_text().replace("(pin passive line", "(pin input line", 1))
        registry_path = self.fixture / "registry.json"
        registry = json.loads(registry_path.read_text())
        for entry in registry["libraries"]:
            if entry["name"] == "symbol-device":
                entry["sha256"] = hashlib.sha256(library.read_bytes()).hexdigest()
        registry_path.write_text(json.dumps(registry))
        paths = self.paths()
        tree = parse_quoted(paths["schematic"].read_text())
        inputs = [pin for pin in descendants(one(tree, "lib_symbols"), "pin") if pin[1] == "input"]
        self.assertEqual(len(inputs), 1)
        self.assertIs(type(inputs[0][1]), str)
        self.assertIsInstance(one(inputs[0], "number")[1], Quoted)
        self.assertIsInstance(one(inputs[0], "name")[1], Quoted)
        xml = ET.parse(paths["netlist"])
        self.assertEqual(xml.find("libparts/libpart[@part='R']/pins/pin[@num='1']").attrib["type"], "input")
        board = parse_quoted(paths["board"].read_text())
        r1 = next(part for part in children(board, "footprint") if any(field[1:3] == ["Reference", "R1"] for field in children(part, "property")))
        self.assertEqual(one(next(pad for pad in children(r1, "pad") if pad[1] == "1"), "pintype")[1], "input")

    def test_board_and_active_libraries_omit_models_but_captured_originals_retain_them(self):
        bound = self.bound()
        paths = self.paths(bound)
        self.assertEqual(descendants(parse_quoted(paths["board"].read_text()), "model"), [])
        snapshots = [path for key, path in paths.items() if key.startswith("footprint_library_")]
        self.assertTrue(all(not descendants(parse_quoted(path.read_text()), "model") for path in snapshots))
        self.assertTrue(all(descendants(binding.footprint_tree, "model") for binding in bound.components.values()))
        self.assertTrue(all(b"(model " in content for alias, content in bound.source_contents.items() if bound.provenance["sources"][alias]["kind"] == "footprint"))
        manifest = json.loads(paths["manifest"].read_text())
        self.assertTrue(all(component["omitted_3d_models"] for component in manifest["components"]))

    def test_explicit_no_connect_is_a_marker_and_never_a_copper_net(self):
        raw = self.design.to_dict()
        del raw["root"]["components"][1]["pins"]["2"]
        bindings = json.loads((self.fixture / "bindings.json").read_text())
        bindings["components"]["board.r1"]["no_connect_pins"] = ["2"]
        (self.fixture / "bindings.json").write_text(json.dumps(bindings))
        paths = self.paths(self.bound(Design.from_dict(raw)))
        schematic = parse_quoted(paths["schematic"].read_text())
        self.assertEqual(len(children(schematic, "no_connect")), 1)
        board = parse_quoted(paths["board"].read_text())
        r1 = next(part for part in children(board, "footprint") if any(field[1:3] == ["Reference", "R1"] for field in children(part, "property")))
        pad = next(pad for pad in children(r1, "pad") if pad[1] == "2")
        self.assertEqual(children(pad, "net"), [])
        self.assertTrue(any("no-connect" in text for text in json.loads(paths["manifest"].read_text())["limitations"]))

    def test_dnp_components_remain_bound_and_have_native_dnp_flags(self):
        raw = self.design.to_dict()
        raw["root"]["components"][2]["fitted"] = False
        paths = self.paths(self.bound(Design.from_dict(raw)))
        schematic = parse_quoted(paths["schematic"].read_text())
        r2 = next(part for part in children(schematic, "symbol") if any(field[1:3] == ["Reference", "R2"] for field in children(part, "property")))
        self.assertEqual(one(r2, "dnp")[1], "yes")
        self.assertIn('dnp', next(one(part, "attr") for part in children(parse_quoted(paths["board"].read_text()), "footprint") if any(field[1:3] == ["Reference", "R2"] for field in children(part, "property"))))
        self.assertEqual(len(ET.parse(paths["netlist"]).findall("components/comp")), 3)

    def test_no_fallback_for_bottom_or_unsupported_unit_or_conflicting_definition(self):
        for mutation in (lambda bound: setattr(bound.components["board.r1"].component.position, "side", "bottom"),
                         lambda bound: setattr(bound.components["board.r1"], "unit", 2),
                         lambda bound: bound.components["board.r2"].symbol_tree.append(["property", Quoted("Unexpected"), Quoted("different")])):
            bound = self.bound()
            mutation(bound)
            destination = self.root / "invalid"
            with self.subTest(mutation=mutation), self.assertRaises(LibraryGenerationError):
                generate_bound(bound, destination)
            self.assertFalse(destination.exists())

    def test_uuid_and_net_code_tables_are_unique_and_deterministic(self):
        tree = parse_quoted(self.paths()["board"].read_text())
        uuids = [node[1] for node in descendants(tree, "uuid")]
        self.assertEqual(len(uuids), len(set(uuids)))
        declarations = children(tree, "net")
        self.assertEqual(declarations, [["net", "0", ""], ["net", "1", "GND"], ["net", "2", "VIN"], ["net", "3", "VOUT"]])
        codes = {node[1]: node[2] for node in declarations}
        for pad in descendants(tree, "pad"):
            if children(pad, "net"):
                net = one(pad, "net")
                self.assertEqual(codes[net[1]], net[2])

    def test_output_symlink_fails_before_any_other_artifact_is_written(self):
        destination = self.root / "symlink-output"
        destination.mkdir()
        outside = self.root / "untouched.txt"
        outside.write_text("keep")
        (destination / "sym-lib-table").symlink_to(outside)
        with self.assertRaises(LibraryGenerationError):
            generate_bound(self.bound(), destination)
        self.assertEqual(outside.read_text(), "keep")
        self.assertEqual([path.name for path in destination.iterdir()], ["sym-lib-table"])

    def test_numbered_mechanical_npth_pads_never_join_electrical_nets(self):
        footprint = self.fixture / "libraries" / "R_0603_1608Metric.kicad_mod"
        text = footprint.read_text()
        mechanical = '\n(pad "1" np_thru_hole circle (at 3 0) (size 1.5 1.5) (drill 1) (layers "*.Cu" "*.Mask"))\n(pad "M1" np_thru_hole circle (at -3 0) (size 1.5 1.5) (drill 1) (layers "*.Cu" "*.Mask"))\n'
        footprint.write_text(text[:text.rfind(")")] + mechanical + ")\n")
        registry_path = self.fixture / "registry.json"
        registry = json.loads(registry_path.read_text())
        for entry in registry["libraries"]:
            if entry["name"] == "footprint-r0603":
                entry["sha256"] = hashlib.sha256(footprint.read_bytes()).hexdigest()
        registry_path.write_text(json.dumps(registry))
        paths = self.paths()
        self.assertTrue(check_parity(self.design, paths["netlist"], paths["board"]).passed)
        tree = parse_quoted(paths["board"].read_text())
        holes = [pad for pad in descendants(tree, "pad") if pad[2] == "np_thru_hole"]
        self.assertEqual(len(holes), 4)
        self.assertTrue(all(not children(pad, "net") and not children(pad, "pintype") for pad in holes))

    def test_nonzero_source_pad_angles_remain_local_at_rotated_placements(self):
        footprint = self.fixture / "libraries" / "R_0603_1608Metric.kicad_mod"
        footprint.write_text(footprint.read_text().replace("(at -0.825 0)", "(at -0.825 0 37.5)"))
        registry_path = self.fixture / "registry.json"
        registry = json.loads(registry_path.read_text())
        for entry in registry["libraries"]:
            if entry["name"] == "footprint-r0603":
                entry["sha256"] = hashlib.sha256(footprint.read_bytes()).hexdigest()
        registry_path.write_text(json.dumps(registry))
        tree = parse_quoted(self.paths()["board"].read_text())
        checked = set()
        for placed in children(tree, "footprint"):
            reference = next(field[2] for field in children(placed, "property") if field[1] == "Reference")
            if reference in {"R1", "R2"}:
                pad = next(pad for pad in children(placed, "pad") if pad[1] == "1")
                self.assertEqual(one(pad, "at"), ["at", "-0.825", "0", "37.5"])
                self.assertIn(float(one(placed, "at")[3]), {90, 270})
                checked.add(reference)
        self.assertEqual(checked, {"R1", "R2"})

    def test_text_box_angles_stay_local_while_ordinary_text_angles_are_absolute(self):
        footprint = self.fixture / "libraries" / "R_0603_1608Metric.kicad_mod"
        text = footprint.read_text()
        box = '\n(fp_text_box "Pinned text box" (start -2 -4) (end 2 -2) (angle 30) (layer "F.Fab") (effects (font (size 1 1))) (stroke (width 0.05) (type solid)))\n'
        footprint.write_text(text[:text.rfind(")")] + box + ")\n")
        registry_path = self.fixture / "registry.json"
        registry = json.loads(registry_path.read_text())
        for entry in registry["libraries"]:
            if entry["name"] == "footprint-r0603":
                entry["sha256"] = hashlib.sha256(footprint.read_bytes()).hexdigest()
        registry_path.write_text(json.dumps(registry))
        tree = parse_quoted(self.paths()["board"].read_text())
        checked = set()
        for placed in children(tree, "footprint"):
            reference = next(field[2] for field in children(placed, "property") if field[1] == "Reference")
            if reference in {"R1", "R2"}:
                self.assertEqual(one(one(placed, "fp_text_box"), "angle"), ["angle", "30"])
                ref_field = next(field for field in children(placed, "property") if field[1] == "Reference")
                self.assertEqual(float(one(ref_field, "at")[3]), float(one(placed, "at")[3]))
                checked.add(reference)
        self.assertEqual(checked, {"R1", "R2"})

    def test_nominal_symbol_anchors_and_connection_labels_stay_on_127_grid(self):
        tree = parse_quoted(self.paths()["schematic"].read_text())
        instances = children(tree, "symbol")
        self.assertEqual(one(instances[0], "at")[1:3], ["63.5", "25.4"])
        for node in [*instances, *children(tree, "global_label"), *children(tree, "no_connect")]:
            for value in one(node, "at")[1:3]:
                units = float(value) / 1.27
                self.assertAlmostEqual(units, round(units), places=9)

    def test_non_grid_pin_span_does_not_shift_later_anchors_off_grid(self):
        library = self.fixture / "libraries" / "Device.kicad_sym"
        library.write_text(library.read_text().replace("(at 0 -3.81 90)", "(at 0 -4.2 90)"))
        registry_path = self.fixture / "registry.json"
        registry = json.loads(registry_path.read_text())
        for entry in registry["libraries"]:
            if entry["name"] == "symbol-device":
                entry["sha256"] = hashlib.sha256(library.read_bytes()).hexdigest()
        registry_path.write_text(json.dumps(registry))
        tree = parse_quoted(self.paths()["schematic"].read_text())
        instances = children(tree, "symbol")
        for instance in instances:
            for value in one(instance, "at")[1:3]:
                units = float(value) / 1.27
                self.assertAlmostEqual(units, round(units), places=9)
        # Actual source pins remain exact; the generator does not conceal an
        # off-grid library endpoint by distorting the imported symbol geometry.
        y = float(one(instances[1], "at")[2])
        self.assertTrue(any(abs(float(one(label, "at")[2]) - y - 4.2) < 1e-9
                            for label in children(tree, "global_label")))

    def test_native_default_ignored_checks_are_enabled_without_exclusions(self):
        project = json.loads(self.paths()["project"].read_text())
        erc = project["erc"]
        self.assertEqual(erc["rule_severities"], {
            "single_global_label": "error", "four_way_junction": "error",
            "simulation_model_issue": "error", "footprint_filter": "error",
        })
        self.assertEqual(erc["erc_exclusions"], [])
        drc = project["board"]["design_settings"]
        self.assertEqual(drc["rule_severities"], {
            "missing_courtyard": "error", "track_not_centered_on_via": "error",
            "tuning_profile_track_geometries": "error", "footprint_filters_mismatch": "error",
            "footprint_type_mismatch": "error",
        })
        self.assertEqual(drc["drc_exclusions"], [])


if __name__ == "__main__":
    unittest.main()
