"""Require real KiCad hierarchy exports, exact instance paths and child proofs.

The fixture has a root component, two nested component sheets and two empty
sheets, including the same leaf name under different parents. Physical globals
stay unchanged. Native DRC remains blocking: this is not fabrication evidence.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
from typing import Any
import xml.etree.ElementTree as ET

from ..bindings import load_bindings
from ..bound_parity import check_bound_parity
from ..hierarchy import schematic_snapshot
from ..model import load_design, walk_components
from ..parity import parse_netlist
from ..sexpr import parse_quoted
from .kicad import KiCadBackend, generate
from .library_kicad import generate_bound
from .runner import ToolRunner
from .smoke_libraries import verify_board_geometry, verify_native_netlist, verify_pcbnew_geometry


def _children(node: list, tag: str) -> list[list]:
    return [field for field in node[1:] if isinstance(field, list) and field and field[0] == tag]


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")


def _capture_fixture(source: Path, destination: Path, *, no_connect: bool = False) -> None:
    """Only rearrange explicit model declarations; retain captured source bytes."""
    shutil.copytree(source, destination)
    model = json.loads((destination / "design.json").read_text(encoding="utf-8"))
    bindings = json.loads((destination / "bindings.json").read_text(encoding="utf-8"))
    parts = {part["id"]: part for part in model["root"]["components"]}
    if set(parts) != {"j1", "r1", "r2"}:
        raise ValueError("Hierarchy smoke requires the pinned three-part divider fixture")
    if no_connect:
        for component, number in (("r1", "2"), ("r2", "1")):
            del parts[component]["pins"][number]
            bindings["components"][f"board.{component}"]["no_connect_pins"] = [number]
    model["root"]["components"] = [parts["j1"]]
    model["root"]["children"] = [
        {"id": "filter", "components": [parts["r1"]],
         "children": [{"id": "return", "components": [parts["r2"]]}]},
        {"id": "spare", "children": [{"id": "return"}]},
    ]
    entries = bindings["components"]
    entries["board.filter.r1"] = entries.pop("board.r1")
    entries["board.filter.return.r2"] = entries.pop("board.r2")
    _write_json(destination / "design.json", model)
    _write_json(destination / "bindings.json", bindings)


def verify_native_paths(files: dict[str, Path], native: Path, design) -> dict:
    """Compare actual native sheet and component paths with model ownership.

    Native XML paths exclude the root file UUID. PCB instance paths include it.
    Child file UUIDs must never be substituted for the sheet-object UUIDs.
    """
    metadata = json.loads(files["manifest"].read_text(encoding="utf-8"))["hierarchy"]
    sheets = metadata["sheets"]
    by_block = {sheet["block_path"]: sheet for sheet in sheets}
    root = ET.parse(native).getroot()
    actual = [(sheet.get("number"), sheet.get("name"), sheet.get("tstamps"))
              for sheet in root.findall("design/sheet")]
    expected = [(sheet["page"], sheet["human_path"], sheet["netlist_path"]) for sheet in sheets]
    if sorted(actual) != sorted(expected):
        raise ValueError(f"Native sheet ownership/page paths differ: {actual!r} versus {expected!r}")
    components = {item.get("ref"): item for item in root.findall("components/comp")}
    board = parse_quoted(files["board"].read_text(encoding="utf-8"))
    footprints = {next(field[2] for field in _children(item, "property") if field[1] == "Reference"): item
                  for item in _children(board, "footprint")}
    owners = {}
    for path, component in walk_components(design):
        sheet = by_block[path.rsplit(".", 1)[0]]
        source_tree = parse_quoted(files[sheet["key"]].read_text(encoding="utf-8"))
        symbol = next(item for item in _children(source_tree, "symbol")
                      if any(field[1:3] == ["Reference", component.reference]
                             for field in _children(item, "property")))
        symbol_uuid = _children(symbol, "uuid")[0][1]
        native_component = components[component.reference]
        native_path = native_component.find("sheetpath")
        if (native_path is None or native_path.get("names") != sheet["human_path"]
                or native_path.get("tstamps") != sheet["netlist_path"]
                or native_component.findtext("tstamps") != symbol_uuid):
            raise ValueError(f"Native {component.reference} lost its exact hierarchical instance identity")
        board_path = _children(footprints[component.reference], "path")[0][1]
        if board_path != sheet["instance_path"] + "/" + symbol_uuid:
            raise ValueError(f"PCB {component.reference} has the wrong hierarchical instance path")
        owners[component.reference] = sheet["human_path"]
    if set(components) != set(owners) or set(footprints) != set(owners):
        raise ValueError("Hierarchy has missing or unexpected component references")
    return {"status": "passed", "native": True, "sheet_count": len(sheets), "component_sheets": owners,
            "root_uuid_excluded_from_xml": True, "root_uuid_included_in_pcb": True}


class _ChildMutationRunner:
    """Return real tool results, then deliberately tamper with a child file."""
    def __init__(self, child: Path, stage: str):
        self.real = ToolRunner()
        self.child, self.stage, self.mutated = child, stage, False

    def run(self, executable, args, cwd, timeout=120):
        result = self.real.run(executable, args, cwd, timeout=timeout)
        matches = (args[:2] == ["sch", "erc"] if self.stage == "erc"
                   else args[:3] == ["sch", "export", "netlist"])
        if matches and result.executed and result.returncode in (0, 5):
            with self.child.open("ab") as stream:
                stream.write(b"\n; deliberate post-native child mutation\n")
            self.mutated = True
        return result


def _prove_child_mutations(backend: KiCadBackend, files: dict[str, Path], output: Path) -> dict:
    child = next(path for key, path in files.items() if key.startswith("schematic_sheet_"))
    original = child.read_bytes()
    snapshot = schematic_snapshot(files["schematic"])
    outcomes = {}
    for stage in ("netlist", "erc"):
        runner = _ChildMutationRunner(child, stage)
        mutating = KiCadBackend(runner=runner, executable=backend.executable, timeout=180)
        try:
            if stage == "netlist":
                target = output / "tampered-export.net"
                result = mutating.export_netlist(files["schematic"], target)
                if target.exists():
                    raise RuntimeError("A child-mutated native netlist was published")
            else:
                result = mutating.check(files["board"], files["schematic"], output / "tampered-checks")["erc"]
                if Path(result["report_path"]).exists():
                    raise RuntimeError("A child-mutated native ERC report was published")
            changed = schematic_snapshot(files["schematic"])
            if (not runner.mutated or not result.get("executed") or result["status"] != "failed"
                    or "closure changed" not in result.get("reason", "")
                    or changed["input_sha256"] == snapshot["input_sha256"]
                    or changed["input_root_sha256"] != snapshot["input_root_sha256"]):
                raise RuntimeError(f"Actual native {stage} child mutation did not fail its closure proof")
            outcomes[stage] = {"evidence": result, "mutated_closure": changed,
                               "root_bytes_unchanged": True, "native_output_rejected": True}
        finally:
            child.write_bytes(original)
        if schematic_snapshot(files["schematic"]) != snapshot:
            raise RuntimeError("Hierarchy smoke failed to restore its deliberate mutation")
    return outcomes


def run_smoke(output_dir: Path, fixture_dir: Path | None = None) -> dict[str, Any]:
    destination = Path(output_dir).resolve()
    if destination.exists():
        raise ValueError("Hierarchy smoke output must be a new directory")
    destination.mkdir(parents=True)
    backend = KiCadBackend(timeout=180)
    report: dict[str, Any] = {"status": "failed", "fabrication_ready": False, "routed": False,
                              "diagnostic": backend.diagnose(), "variants": {}}
    try:
        if not report["diagnostic"].get("supported"):
            raise RuntimeError("Real KiCad 9 or 10 is mandatory; hierarchy smoke cannot be mocked or skipped")
        fixture = (Path(fixture_dir).resolve() if fixture_dir is not None else
                   Path(__file__).resolve().parents[3] / "examples/library_divider")
        if destination.is_relative_to(fixture) or fixture.is_relative_to(destination):
            raise ValueError("Hierarchy smoke fixture and output must be separate")
        for mode in ("generic", "bound", "bound-no-connect"):
            output = destination / mode
            captured = output / "fixture"
            output.mkdir()
            _capture_fixture(fixture, captured, no_connect=mode == "bound-no-connect")
            design = load_design(captured / "design.json")
            bound = load_bindings(design, captured / "bindings.json")
            files = (generate(design.to_dict(), output / "source", hierarchical=True) if mode == "generic" else
                     generate_bound(bound, output / "source", hierarchical=True))
            item: dict = {"closure": schematic_snapshot(files["schematic"])}
            report["variants"][mode] = item
            exported = backend.export_netlist(files["schematic"], output / "native.net")
            item["netlist_export"] = exported
            if exported["status"] != "passed" or not exported.get("executed"):
                raise RuntimeError(f"Real {mode} hierarchical netlist export failed")
            item["instance_paths"] = verify_native_paths(files, Path(exported["path"]), design)
            if mode == "generic":
                native = parse_netlist(exported["path"])
                expected_nets = {reference: dict(component.pins) for _, component in walk_components(design)
                                 for reference in [component.reference]}
                if {reference: component["pins"] for reference, component in native.items()} != expected_nets:
                    raise RuntimeError("Generic native hierarchy changed explicit physical pin/global-net mappings")
            else:
                item["geometry"] = verify_board_geometry(bound, files["board"])
                item["pcbnew_geometry"] = verify_pcbnew_geometry(bound, files["board"])
                parity = check_bound_parity(bound, exported["path"], files["board"])
                item["parity"] = parity.to_dict()
                if parity.blocking:
                    raise RuntimeError(f"Real {mode} hierarchy pin-net parity failed")
                if mode == "bound":
                    item["connectivity"] = verify_native_netlist(bound, Path(exported["path"]))
                elif parity.metadata.get("no_connect_pin_count") != 2:
                    raise RuntimeError("Hierarchical explicit NC pins were not both verified")
            checks = backend.check(files["board"], files["schematic"], output / "checks")
            item["checks"] = checks
            for stage, evidence in checks.items():
                if not evidence.get("executed") or "native_report" not in evidence:
                    raise RuntimeError(f"Actual {mode} {stage} did not provide native report evidence")
            if mode != "generic" and checks["erc"]["status"] != "passed":
                raise RuntimeError(f"Real {mode} hierarchy must pass ERC without omitted checks")
            drc = checks["drc"]
            if drc["status"] != "failed" or not drc["native_report"].get("unconnected_items"):
                raise RuntimeError(f"Real {mode} hierarchy lost blocking unrouted DRC evidence")
            if (checks["erc"]["input_sha256"] != exported["input_sha256"]
                    or exported["input_sha256"] != item["closure"]["input_sha256"]):
                raise RuntimeError("Hierarchical ERC/export did not bind the same complete input closure")
            if mode != "bound-no-connect":
                item["exports"] = backend.export(files["board"], files["schematic"], output / "exports")
                svg = item["exports"]["products"]["schematic_svg"]
                if (svg["status"] != "passed" or len(svg["paths"]) != 5
                        or svg["input_sha256"] != exported["input_sha256"]):
                    raise RuntimeError(f"Real {mode} hierarchy did not export all five SVG pages")
            if mode == "bound":
                item["child_mutations"] = _prove_child_mutations(backend, files, output)
        report.update(status="passed", interpretation="Real hierarchical grammar, instance paths, global nets and bound NC parity verified; child mutation rejected; unrouted DRC remains blocking")
    except (OSError, ValueError, RuntimeError, KeyError, StopIteration, TypeError, ET.ParseError) as exc:
        report["reason"] = f"{type(exc).__name__}: {exc}"
    _write_json(destination / "smoke-report.json", report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--fixture", type=Path)
    args = parser.parse_args()
    result = run_smoke(args.output, args.fixture)
    print(json.dumps({"status": result["status"], "fabrication_ready": False, "reason": result.get("reason")}))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
