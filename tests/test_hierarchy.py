"""Deterministic hierarchy and negative closure evidence; no native-success claim."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from pcb_dev.backends import KiCadBackend, ToolResult, generate, generate_bound
from pcb_dev.bindings import load_bindings
from pcb_dev.hierarchy import HierarchyError, assert_snapshot, plan_sheets, schematic_snapshot
from pcb_dev.model import Design, load_design
from pcb_dev.parity import check_parity, parse_board, parse_netlist
from pcb_dev.pipeline import build_preview
from pcb_dev.sexpr import Quoted, dumps, parse_quoted


FIXTURE = Path(__file__).resolve().parents[1] / "examples/library_divider"


def children(tree, tag):
    return [field for field in tree[1:] if isinstance(field, list) and field and field[0] == tag]


def one(tree, tag):
    fields = children(tree, tag)
    if len(fields) != 1:
        raise AssertionError(tag)
    return fields[0]


def nested_fixture(directory):
    """Keep upstream bytes/licenses intact, relocate only model declarations."""
    shutil.copytree(FIXTURE, directory)
    data = json.loads((directory / "design.json").read_text(encoding="utf-8"))
    j1, r1, r2 = data["root"]["components"]
    data["root"]["components"] = [j1]
    data["root"]["children"] = [
        {"id": "filter", "components": [r1], "children": [{"id": "return", "components": [r2]}]},
        {"id": "spare", "children": [{"id": "return"}]},
    ]
    (directory / "design.json").write_text(json.dumps(data), encoding="utf-8")
    bindings = json.loads((directory / "bindings.json").read_text(encoding="utf-8"))
    entries = bindings["components"]
    entries["board.filter.r1"] = entries.pop("board.r1")
    entries["board.filter.return.r2"] = entries.pop("board.r2")
    (directory / "bindings.json").write_text(json.dumps(bindings), encoding="utf-8")
    return data


class ProofRunner:
    """Synthetic external-tool output plus a real on-disk child mutation."""
    def __init__(self, child=None, mutate=None, svg_count=5):
        self.child, self.mutate, self.svg_count = child, mutate, svg_count
        self.calls = []

    def run(self, executable, args, cwd, timeout=120):
        self.calls.append(args)
        command = (executable, *args)
        if args == ["--version"]:
            return ToolResult(command, "success", 0, "10.0.2", executed=True)
        target = Path(args[args.index("--output") + 1])
        stage = args[1] if args[1] in ("erc", "drc") else args[2]
        if stage == self.mutate:
            self.child.write_text(self.child.read_text(encoding="utf-8") + "\n; external mutation\n", encoding="utf-8")
        if stage in ("erc", "drc"):
            data = {"source": Path(args[-1]).name, "date": "2026-10-17T12:00:00Z",
                    "coordinate_units": "mm", "kicad_version": "10.0.2"}
            data.update({"sheets": [{"path": "/", "violations": []}]} if stage == "erc" else
                        {"violations": [], "unconnected_items": [], "schematic_parity": []})
            target.write_text(json.dumps(data), encoding="utf-8")
        elif stage == "netlist":
            root = ET.Element("export", version="E")
            ET.SubElement(ET.SubElement(root, "design"), "source").text = Path(args[-1]).name
            ET.SubElement(root, "components")
            ET.SubElement(root, "nets")
            target.write_text(ET.tostring(root, encoding="unicode"), encoding="utf-8")
        elif stage == "svg":
            for index in range(self.svg_count):
                (target / f"sheet-{index}.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"/>', encoding="utf-8")
        return ToolResult(command, "success", 0, executed=True)


class HierarchyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.fixture = self.root / "fixture"
        self.data = nested_fixture(self.fixture)
        self.design = load_design(self.fixture / "design.json")
        self.bound = load_bindings(self.design, self.fixture / "bindings.json")

    def tearDown(self):
        self.temporary.cleanup()

    def files(self, mode="bound", hierarchical=True, directory="output"):
        if mode == "bound":
            return generate_bound(self.bound, self.root / directory, hierarchical=hierarchical)
        return generate(self.data, self.root / directory, hierarchical=hierarchical)

    def metadata(self, files):
        return json.loads(files["manifest"].read_text(encoding="utf-8"))["hierarchy"]

    def sheet_files(self, files):
        return {key: path for key, path in files.items() if key == "schematic" or key.startswith("schematic_sheet_")}

    def test_both_modes_create_one_distinct_sheet_for_every_block_including_empty(self):
        for mode in ("generic", "bound"):
            with self.subTest(mode=mode):
                files = self.files(mode, directory=mode)
                metadata = self.metadata(files)
                self.assertEqual(len(self.sheet_files(files)), 5)
                self.assertEqual({sheet["block_path"] for sheet in metadata["sheets"]},
                                 {path for path, _ in self.design.walk_blocks()})
                self.assertEqual(len({sheet["filename"].casefold() for sheet in metadata["sheets"]}), 5)
                for sheet in metadata["sheets"]:
                    tree = parse_quoted(files[sheet["key"]].read_text(encoding="utf-8"))
                    references = {field[2] for symbol in children(tree, "symbol")
                                  for field in children(symbol, "property") if field[1] == "Reference"}
                    expected = {component.reference for path, block in self.design.walk_blocks()
                                if path == sheet["block_path"] for component in block.components}
                    self.assertEqual(references, expected)
                    self.assertEqual(one(tree, "uuid")[1], sheet["file_uuid"])
                snapshot = schematic_snapshot(files["schematic"])
                self.assertEqual(len(snapshot["input_closure"]["files"]), 5)
                self.assertTrue(snapshot["input_closure"]["hierarchical"])

    def test_hierarchy_is_portable_deterministic_and_manifest_covers_every_child(self):
        first, second = self.files(directory="first"), self.files(directory="second")
        self.assertEqual(first.keys(), second.keys())
        for key in first:
            self.assertEqual(first[key].read_bytes(), second[key].read_bytes(), key)
        self.assertEqual(schematic_snapshot(first["schematic"]), schematic_snapshot(second["schematic"]))
        manifest = json.loads(first["manifest"].read_text(encoding="utf-8"))
        for key, path in self.sheet_files(first).items():
            self.assertEqual(manifest["artifacts"][key]["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
            self.assertNotIn(str(self.root), path.read_text(encoding="utf-8"))
        self.assertFalse(manifest["fabrication_ready"])
        self.assertFalse(manifest["routed"])

    def test_sheet_parent_instances_symbol_instances_board_paths_and_xml_agree(self):
        files = self.files()
        metadata = self.metadata(files)
        by_path = {sheet["block_path"]: sheet for sheet in metadata["sheets"]}
        board = parse_quoted(files["board"].read_text(encoding="utf-8"))
        footprints = {next(field[2] for field in children(item, "property") if field[1] == "Reference"): item
                      for item in children(board, "footprint")}
        xml = ET.parse(files["netlist"])
        for sheet in metadata["sheets"]:
            tree = parse_quoted(files[sheet["key"]].read_text(encoding="utf-8"))
            for symbol in children(tree, "symbol"):
                reference = next(field[2] for field in children(symbol, "property") if field[1] == "Reference")
                self.assertEqual(one(one(one(symbol, "instances"), "project"), "path")[1], sheet["instance_path"])
                self.assertEqual(one(footprints[reference], "path")[1], sheet["instance_path"] + "/" + one(symbol, "uuid")[1])
                component = xml.find(f"components/comp[@ref='{reference}']/sheetpath")
                self.assertEqual(component.get("names"), sheet["human_path"])
                self.assertEqual(component.get("tstamps"), sheet["netlist_path"])
            for child in children(tree, "sheet"):
                target = next(field[2] for field in children(child, "property") if field[1] == "Sheetfile")
                child_sheet = next(item for item in metadata["sheets"] if item["filename"] == target)
                self.assertEqual(one(one(one(child, "instances"), "project"), "path")[1], sheet["instance_path"])
                self.assertEqual(child_sheet["parent_path"], sheet["block_path"])
                self.assertEqual(one(child, "uuid")[1], child_sheet["sheet_uuid"])
                self.assertNotEqual(child_sheet["sheet_uuid"], child_sheet["file_uuid"])
        self.assertEqual(by_path["board.filter.return"]["human_path"], "/filter/return/")

    def test_global_connected_nets_and_physical_geometry_are_preserved(self):
        flat, hierarchical = self.files(hierarchical=False, directory="flat"), self.files(directory="hierarchical")
        self.assertEqual(parse_netlist(flat["netlist"]), parse_netlist(hierarchical["netlist"]))
        self.assertEqual(parse_board(flat["board"]), parse_board(hierarchical["board"]))
        self.assertTrue(check_parity(self.design, hierarchical["netlist"], hierarchical["board"]).passed)
        from pcb_dev.backends.smoke_libraries import verify_board_geometry
        self.assertEqual(verify_board_geometry(self.bound, hierarchical["board"])["status"], "passed")

    def test_default_flat_files_are_identical_to_explicit_false_and_keep_raw_hash(self):
        for mode in ("generic", "bound"):
            explicit = self.files(mode, False, mode + "-explicit")
            default = (generate_bound(self.bound, self.root / (mode + "-default")) if mode == "bound" else
                       generate(self.data, self.root / (mode + "-default")))
            self.assertEqual(explicit.keys(), default.keys())
            for key in explicit:
                self.assertEqual(explicit[key].read_bytes(), default[key].read_bytes())
            snapshot = schematic_snapshot(default["schematic"])
            self.assertEqual(snapshot["input_sha256"], hashlib.sha256(default["schematic"].read_bytes()).hexdigest())
            self.assertFalse(snapshot["input_closure"]["hierarchical"])

    def test_symbol_identities_survive_layout_switch_and_sheet_identity_survives_sibling_add(self):
        flat, hierarchical = self.files(hierarchical=False, directory="flat"), self.files(directory="hierarchical")
        flat_tree = parse_quoted(flat["schematic"].read_text(encoding="utf-8"))
        flat_ids = {one(symbol, "uuid")[1] for symbol in children(flat_tree, "symbol")}
        hierarchical_ids = {one(symbol, "uuid")[1] for path in self.sheet_files(hierarchical).values()
                            for symbol in children(parse_quoted(path.read_text(encoding="utf-8")), "symbol")}
        self.assertEqual(flat_ids, hierarchical_ids)
        root_uuid = one(parse_quoted(hierarchical["schematic"].read_text(encoding="utf-8")), "uuid")[1]
        before = plan_sheets(self.design, "safe", root_uuid)
        changed = self.design.to_dict()
        changed["root"]["children"].insert(0, {"id": "added"})
        after = plan_sheets(Design.from_dict(changed), "safe", root_uuid)
        for sheet in before.sheets:
            same = next(item for item in after.sheets if item.block_path == sheet.block_path)
            self.assertEqual((sheet.filename, sheet.file_uuid, sheet.sheet_uuid, sheet.instance_path),
                             (same.filename, same.file_uuid, same.sheet_uuid, same.instance_path))

    def test_child_mutation_changes_closure_while_root_bytes_are_unchanged(self):
        files = self.files()
        before = schematic_snapshot(files["schematic"])
        child = next(path for key, path in files.items() if key.startswith("schematic_sheet_"))
        child.write_text(child.read_text(encoding="utf-8") + "\n; child edit\n", encoding="utf-8")
        after = schematic_snapshot(files["schematic"])
        self.assertEqual(before["input_root_sha256"], after["input_root_sha256"])
        self.assertNotEqual(before["input_sha256"], after["input_sha256"])
        with self.assertRaises(HierarchyError):
            assert_snapshot(files["schematic"], before)

    def test_pipeline_bundles_all_sheets_and_verifies_after_relocation(self):
        from pcb_dev.artifacts import verify_manifest
        destination = self.root / "build"
        build_preview(self.fixture / "design.json", destination, bindings=self.fixture / "bindings.json", hierarchical=True)
        lock = json.loads((destination / "build-lock.json").read_text(encoding="utf-8"))
        self.assertEqual(lock["schematic_layout"], "hierarchical-global-nets")
        self.assertEqual(len([key for key in lock["generated"] if key.startswith("schematic")]), 5)
        self.assertEqual(verify_manifest(destination)["schema_version"], 1)
        shutil.rmtree(self.fixture)
        moved = self.root / "moved"
        destination.rename(moved)
        self.assertEqual(verify_manifest(moved)["schema_version"], 1)
        self.assertEqual(len(schematic_snapshot(moved / lock["generated"]["schematic"])["input_closure"]["files"]), 5)

    def test_native_smoke_path_validator_rejects_child_file_uuid_in_instance_path(self):
        # Synthetic XML tests the smoke validator; native CLI evidence is only
        # produced by smoke_hierarchy itself in the KiCad CI environment.
        from pcb_dev.backends.smoke_hierarchy import verify_native_paths
        files = self.files()
        document = ET.parse(files["netlist"])
        sheets = self.metadata(files)["sheets"]
        for sheet in sheets:
            ET.SubElement(document.getroot().find("design"), "sheet", {
                "number": sheet["page"], "name": sheet["human_path"], "tstamps": sheet["netlist_path"]})
        target = self.root / "synthetic-paths.net"
        document.write(target)
        self.assertEqual(verify_native_paths(files, target, self.design)["sheet_count"], 5)
        r1 = document.find("components/comp[@ref='R1']/sheetpath")
        owner = next(sheet for sheet in sheets if sheet["block_path"] == "board.filter")
        r1.set("tstamps", "/" + owner["file_uuid"] + "/")
        document.write(target)
        with self.assertRaisesRegex(ValueError, "exact hierarchical instance identity"):
            verify_native_paths(files, target, self.design)

    def test_native_hierarchy_smoke_is_failed_when_engine_is_unavailable(self):
        from pcb_dev.backends.smoke_hierarchy import run_smoke
        with patch("pcb_dev.backends.smoke_hierarchy.KiCadBackend.diagnose",
                   return_value={"supported": False, "status": "unavailable"}):
            report = run_smoke(self.root / "unavailable-smoke")
        self.assertEqual(report["status"], "failed")
        self.assertFalse(report["fabrication_ready"])
        self.assertIn("cannot be mocked or skipped", report["reason"])
        self.assertEqual(report["variants"], {})
        self.assertTrue((self.root / "unavailable-smoke/smoke-report.json").is_file())

    def test_oversized_schematic_is_refused_before_legacy_hash_or_native_execution(self):
        files = self.files()
        runner = ProofRunner()
        backend = KiCadBackend(runner=runner)
        diagnostic = {"status": "passed", "supported": True, "version": "10.0.2"}
        output = self.root / "rejected-checks"
        output.mkdir()
        with patch("pcb_dev.hierarchy.MAX_SHEET_BYTES", 1), patch("pcb_dev.backends.kicad._sha256") as raw_hash:
            erc = backend._check_one("erc", files["schematic"], output, diagnostic)
            exported = backend.export_netlist(files["schematic"], self.root / "rejected.net")
            raw_hash.assert_not_called()
        for evidence in (erc, exported):
            self.assertEqual(evidence["status"], "failed")
            self.assertFalse(evidence["executed"])
            self.assertIsNone(evidence["input_sha256"])
            self.assertIn("oversized", evidence["reason"])
        self.assertEqual(runner.calls, [])

    def test_missing_child_duplicate_reference_cycle_and_duplicate_uuid_fail_closed(self):
        files = self.files()
        root_path = files["schematic"]
        original = root_path.read_text(encoding="utf-8")
        root_tree = parse_quoted(original)
        child = children(root_tree, "sheet")[0]
        filename = next(field for field in children(child, "property") if field[1] == "Sheetfile")
        original_name = filename[2]
        for bad in ("missing.kicad_sch", Quoted(root_path.name)):
            filename[2] = Quoted(bad)
            root_path.write_text(dumps(root_tree), encoding="utf-8")
            with self.assertRaises(HierarchyError):
                schematic_snapshot(root_path)
        filename[2] = original_name
        root_tree.append(deepcopy(child))
        root_path.write_text(dumps(root_tree), encoding="utf-8")
        with self.assertRaises(HierarchyError):
            schematic_snapshot(root_path)
        root_path.write_text(original, encoding="utf-8")
        target = root_path.parent / original_name
        target_tree = parse_quoted(target.read_text(encoding="utf-8"))
        one(target_tree, "uuid")[1] = one(parse_quoted(original), "uuid")[1]
        target.write_text(dumps(target_tree), encoding="utf-8")
        with self.assertRaises(HierarchyError):
            schematic_snapshot(root_path)

    def test_unsafe_relative_external_environment_or_wrong_extension_paths_are_rejected(self):
        files = self.files()
        tree = parse_quoted(files["schematic"].read_text(encoding="utf-8"))
        field = next(field for field in children(children(tree, "sheet")[0], "property") if field[1] == "Sheetfile")
        for name in ("../outside.kicad_sch", "/outside.kicad_sch", "C:/outside.kicad_sch",
                     "${KIPRJMOD}/x.kicad_sch", "nested\\x.kicad_sch", "./x.kicad_sch", "x.txt"):
            field[2] = Quoted(name)
            files["schematic"].write_text(dumps(tree), encoding="utf-8")
            with self.subTest(name=name), self.assertRaises(HierarchyError):
                schematic_snapshot(files["schematic"])

    def test_shared_file_alias_and_bounded_file_count_are_refused(self):
        files = self.files()
        with patch("pcb_dev.hierarchy.MAX_SHEETS", 2), self.assertRaises(HierarchyError):
            schematic_snapshot(files["schematic"])
        tree = parse_quoted(files["schematic"].read_text(encoding="utf-8"))
        first, second = children(tree, "sheet")
        name = next(field[2] for field in children(first, "property") if field[1] == "Sheetfile")
        next(field for field in children(second, "property") if field[1] == "Sheetfile")[2] = name
        files["schematic"].write_text(dumps(tree), encoding="utf-8")
        with self.assertRaises(HierarchyError):
            schematic_snapshot(files["schematic"])

    def test_erc_and_native_export_reject_child_tampering_even_if_root_hash_is_stable(self):
        for stage in ("erc", "netlist"):
            files = self.files(directory=stage)
            child = next(path for key, path in files.items() if key.startswith("schematic_sheet_"))
            runner = ProofRunner(child, mutate=stage)
            backend = KiCadBackend(runner)
            if stage == "erc":
                evidence = backend.check(files["board"], files["schematic"], self.root / "checks")["erc"]
            else:
                evidence = backend.export_netlist(files["schematic"], self.root / "native.net")
            self.assertEqual(evidence["status"], "failed", evidence)
            self.assertTrue(evidence["executed"])
            self.assertIn("closure changed", evidence["reason"])

    def test_native_export_cannot_overwrite_a_child_and_bad_closure_never_runs_erc(self):
        files = self.files()
        child = next(path for key, path in files.items() if key.startswith("schematic_sheet_"))
        runner = ProofRunner()
        result = KiCadBackend(runner).export_netlist(files["schematic"], child)
        self.assertFalse(result["executed"])
        self.assertEqual(runner.calls, [])
        child.unlink()
        result = KiCadBackend(runner).check(files["board"], files["schematic"], self.root / "checks")["erc"]
        self.assertFalse(result["executed"])
        self.assertFalse(any(args[:2] == ["sch", "erc"] for args in runner.calls))

    def test_svg_requires_all_pages_and_stable_child_bytes(self):
        for count, mutation in ((4, None), (5, "svg")):
            files = self.files(directory=str(count))
            child = next(path for key, path in files.items() if key.startswith("schematic_sheet_"))
            result = KiCadBackend(ProofRunner(child, mutate=mutation, svg_count=count)).export(
                files["board"], files["schematic"], self.root / (str(count) + "-export"))
            self.assertEqual(result["products"]["schematic_svg"]["status"], "failed")

    def test_mode_flag_and_overview_bound_are_checked_before_output(self):
        for function, value in ((generate, self.data), (generate_bound, self.bound)):
            with self.assertRaises(ValueError):
                function(value, self.root / "invalid", hierarchical="yes")
        self.assertFalse((self.root / "invalid").exists())
        changed = self.design.to_dict()
        changed["root"]["children"] = [{"id": f"block{i}"} for i in range(22)]
        with self.assertRaises(HierarchyError):
            generate(changed, self.root / "oversized", hierarchical=True)
        self.assertFalse((self.root / "oversized").exists())


if __name__ == "__main__":
    unittest.main()
