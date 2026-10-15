"""Real KiCad smoke for explicit no-connect parity, without mocked success."""

from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path
import shutil
from typing import Any
import xml.etree.ElementTree as ET

from ..bindings import load_bindings
from ..bound_parity import check_bound_parity
from ..model import load_design
from ..sexpr import dumps, parse_quoted
from .kicad import KiCadBackend
from .smoke_libraries import verify_board_geometry, verify_pcbnew_geometry


def _children(node: list, tag: str) -> list[list]:
    return [field for field in node[1:] if isinstance(field, list) and field and field[0] == tag]


def _write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, sort_keys=True, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def run_smoke(output_dir: Path, fixture_dir: Path | None = None) -> dict[str, Any]:
    """Require actual NC exports, a clean ERC and preserved unrouted DRC.

    R1.2 and R2.1 are both explicitly NC in this variant, removing VOUT entirely.
    The other two nets retain two pins each, so no single-label rule is hidden.
    """
    destination = Path(output_dir).resolve()
    if destination.exists():
        raise ValueError("No-connect smoke output must be a new directory")
    destination.mkdir(parents=True)
    backend = KiCadBackend(timeout=180)
    report: dict[str, Any] = {"status": "failed", "fabrication_ready": False, "routed": False,
        "fixture": "pinned-library-divider-explicit-nc-variant", "diagnostic": backend.diagnose()}
    try:
        if not report["diagnostic"].get("supported"):
            raise RuntimeError("Real KiCad 9 or 10 is mandatory; no-connect smoke cannot be mocked or skipped")
        from .library_kicad import generate_bound

        fixture = (Path(fixture_dir).resolve() if fixture_dir is not None
                   else Path(__file__).resolve().parents[3] / "examples/library_divider")
        if destination.is_relative_to(fixture) or fixture.is_relative_to(destination):
            raise ValueError("No-connect smoke fixture and output must be separate")
        captured = destination / "fixture"
        shutil.copytree(fixture, captured)
        design_data = json.loads((captured / "design.json").read_text(encoding="utf-8"))
        bindings_data = json.loads((captured / "bindings.json").read_text(encoding="utf-8"))
        parts = {component["id"]: component for component in design_data["root"]["components"]}
        for component, number in (("r1", "2"), ("r2", "1")):
            del parts[component]["pins"][number]
            bindings_data["components"][f"board.{component}"]["no_connect_pins"] = [number]
        _write_json(captured / "design.json", design_data)
        _write_json(captured / "bindings.json", bindings_data)
        design = load_design(captured / "design.json")
        bound = load_bindings(design, captured / "bindings.json")
        generated = generate_bound(bound, destination / "source")
        report["design_sha256"], report["provenance"] = design.fingerprint(), bound.provenance
        report["geometry"] = verify_board_geometry(bound, generated["board"])
        report["pcbnew_geometry"] = verify_pcbnew_geometry(bound, generated["board"])
        exported = backend.export_netlist(generated["schematic"], destination / "native.net")
        report["native_netlist_export"] = exported
        if exported["status"] != "passed":
            raise RuntimeError("Native no-connect netlist export failed")
        parity = check_bound_parity(bound, exported["path"], generated["board"])
        report["parity"] = parity.to_dict()
        if not parity.passed or parity.metadata["no_connect_pin_count"] != 2:
            raise RuntimeError("Real native no-connect pin identities did not pass bound parity")
        checks = backend.check(generated["board"], generated["schematic"], destination / "checks")
        report["checks"] = checks
        if checks["erc"]["status"] != "passed":
            raise RuntimeError("Explicit no-connect fixture must pass real ERC without exclusions")
        drc = checks["drc"]
        if drc["status"] != "failed" or not drc.get("native_report", {}).get("unconnected_items"):
            raise RuntimeError("No-connect fixture lost its actual unrouted blocking DRC evidence")
        if (exported["input_sha256"] != checks["erc"]["input_sha256"]
                or parity.metadata["board_sha256"] != drc["input_sha256"]):
            raise RuntimeError("No-connect parity and native checks used different input bytes")

        # The omitted-marker counterexample is interpreted by the real engine.
        # It must not become a false success simply because its net name looks
        # like an unconnected placeholder.
        mutated_source = destination / "missing-marker-source"
        shutil.copytree(generated["schematic"].parent, mutated_source)
        schematic = mutated_source / generated["schematic"].name
        tree = parse_quoted(schematic.read_text(encoding="utf-8"))
        markers = _children(tree, "no_connect")
        if len(markers) != 2:
            raise RuntimeError("Expected two explicit source no-connect markers")
        tree.remove(markers[0])
        schematic.write_text(dumps(tree) + "\n", encoding="utf-8")
        missing = backend.export_netlist(schematic, destination / "missing-marker.net")
        report["missing_marker_native_export"] = missing
        if missing["status"] != "passed":
            raise RuntimeError("Native missing-marker counterexample could not be exported")
        missing_parity = check_bound_parity(bound, missing["path"], generated["board"])
        if not missing_parity.blocking:
            raise RuntimeError("Native export without its explicit NC marker incorrectly passed")

        # Edited exports/boards below are deliberate negative parser inputs,
        # recorded separately from native engine evidence.
        mutations = {"missing_marker": {"native_export": True, "report": missing_parity.to_dict()}}
        native_xml = ET.parse(exported["path"]).getroot()
        connected = next(node for node in native_xml.findall("nets/net/node")
                         if not node.get("pintype", "").endswith("+no_connect"))
        connected.set("pintype", connected.get("pintype") + "+no_connect")
        fake_nc = destination / "false-nc-annotation.net"
        ET.ElementTree(native_xml).write(fake_nc, encoding="utf-8", xml_declaration=True)
        false_parity = check_bound_parity(bound, fake_nc, generated["board"])
        if not false_parity.blocking:
            raise RuntimeError("A connected pin falsely annotated NC incorrectly passed")
        mutations["false_nc_annotation"] = {"native_export": False, "report": false_parity.to_dict()}

        board_tree = parse_quoted(generated["board"].read_text(encoding="utf-8"))
        footprints = _children(board_tree, "footprint")
        r1 = next(item for item in footprints if any(field[1:3] == ["Reference", "R1"]
                                                    for field in _children(item, "property")))
        nc_pad = next(pad for pad in _children(r1, "pad") if pad[1] == "2")
        net = next(item for item in _children(board_tree, "net") if item[2] == "VIN")
        nc_pad.append(deepcopy(net))
        wrong_board = destination / "connected-nc-pad.kicad_pcb"
        wrong_board.write_text(dumps(board_tree) + "\n", encoding="utf-8")
        connected_parity = check_bound_parity(bound, exported["path"], wrong_board)
        if not connected_parity.blocking:
            raise RuntimeError("An explicitly NC pad assigned a connected net incorrectly passed")
        mutations["connected_nc_pad"] = {"native_export": False, "report": connected_parity.to_dict()}
        report.update(status="passed", mutations=mutations,
            interpretation="Actual NC export accepted; real omitted-marker export and edited false NC/pad assignments rejected; DRC remains blocking")
    except (ValueError, OSError, RuntimeError, KeyError, StopIteration) as exc:
        report["reason"] = str(exc) or type(exc).__name__
    _write_json(destination / "smoke-report.json", report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--fixture", type=Path)
    args = parser.parse_args(argv)
    result = run_smoke(args.output, args.fixture)
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
