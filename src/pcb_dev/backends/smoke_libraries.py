"""Native KiCad checks for pinned real symbol/footprint geometry.

This is an integration smoke, never a fabrication qualification. Real KiCad
execution is mandatory; unrouted DRC failure is mandatory preserved evidence.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any
import xml.etree.ElementTree as ET

from ..artifacts import sha256_file
from ..model import load_design
from ..parity import check_parity, parse_sexpr
from .kicad import KiCadBackend


_MAX_BOARD_BYTES = 32 * 1024 * 1024


def _children(tree: list, name: str) -> list[list]:
    return [item for item in tree if isinstance(item, list) and item and item[0] == name]


def _one(tree: list, name: str) -> list:
    found = _children(tree, name)
    if len(found) != 1:
        raise ValueError(f"Expected one {name} field in library-backed board")
    return found[0]


def _number(value: Any) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("Nonfinite generated geometry")
    return result


def _close(actual: float, expected: float, name: str) -> None:
    if not math.isclose(actual, expected, rel_tol=0, abs_tol=0.000002):
        raise ValueError(f"{name} differs from pinned library/model: {actual:g} versus {expected:g}")


def _angle(actual: float, expected: float, name: str) -> None:
    error = (actual - expected + 180) % 360 - 180
    if not math.isclose(error, 0, rel_tol=0, abs_tol=0.000002):
        raise ValueError(f"{name} differs from pinned library/model: actual {actual:g} degrees versus expected {expected:g} degrees")


def _reference(footprint: list) -> str:
    found = [str(item[2]) for item in _children(footprint, "property") if len(item) >= 3 and item[1] == "Reference"]
    found += [str(item[2]) for item in _children(footprint, "fp_text") if len(item) >= 3 and item[1] == "reference"]
    if len(found) != 1:
        raise ValueError("Generated footprint has ambiguous reference")
    return found[0]


def verify_board_geometry(bound: Any, board_path: Path) -> dict[str, Any]:
    """Compare generated local pad geometry/pose against pinned inventories.

    This bounded parser check is available everywhere. Native loading and the
    optional pcbnew absolute-coordinate check are separate pieces of evidence.
    """
    if board_path.is_symlink() or not board_path.is_file() or not 0 < board_path.stat().st_size <= _MAX_BOARD_BYTES:
        raise ValueError("Generated board is linked, missing, empty or oversized")
    tree = parse_sexpr(board_path.read_text(encoding="utf-8"))
    if not tree or tree[0] != "kicad_pcb":
        raise ValueError("Generated board root is not kicad_pcb")
    footprints = _children(tree, "footprint")
    by_reference = {_reference(item): item for item in footprints}
    if len(by_reference) != len(footprints):
        raise ValueError("Generated board has duplicate footprint references")
    expected = {item.component.reference: item for item in bound.components.values()}
    if set(by_reference) != set(expected):
        raise ValueError("Generated board footprint references differ from the bound design")
    records = []
    for reference, item in sorted(expected.items()):
        footprint = by_reference[reference]
        position = item.component.position
        if position is None:
            raise ValueError("Native library fixture requires explicit component positions")
        if str(footprint[1]) != item.footprint_id:
            raise ValueError("Generated footprint identity does not match its binding")
        pose = _one(footprint, "at")
        _close(_number(pose[1]), position.x_mm, f"{reference} X")
        _close(_number(pose[2]), position.y_mm, f"{reference} Y")
        _angle(_number(pose[3]) if len(pose) > 3 else 0, position.rotation, f"{reference} rotation")
        if _one(footprint, "layer")[1] != "F.Cu":
            raise ValueError("Library fixture unexpectedly flipped a footprint")
        pads = _children(footprint, "pad")
        by_number = {str(pad[1]): pad for pad in pads}
        if len(by_number) != len(pads):
            raise ValueError("Native fixture expects one physical pad per pin number")
        inventory = {pad.number: pad for pad in item.footprint_inventory.pads}
        if set(by_number) != set(inventory):
            raise ValueError("Generated pad numbers differ from the pinned footprint")
        for number, original in sorted(inventory.items()):
            pad = by_number[number]
            if pad[2:4] != [original.type, original.shape]:
                raise ValueError("Real pad type or shape was replaced")
            pose = _one(pad, "at")
            _close(_number(pose[1]), original.x_mm, f"{reference}.{number} local X")
            _close(_number(pose[2]), original.y_mm, f"{reference}.{number} local Y")
            # Board-file pad angles are local to the footprint. KiCad applies
            # the placement rotation when loading native absolute geometry.
            _angle(_number(pose[3]) if len(pose) > 3 else 0, original.rotation, f"{reference}.{number} local pad rotation")
            size = _one(pad, "size")
            _close(_number(size[1]), original.size_x_mm, f"{reference}.{number} pad width")
            _close(_number(size[2]), original.size_y_mm, f"{reference}.{number} pad height")
            if list(_one(pad, "layers")[1:]) != original.layers:
                raise ValueError("Real pad copper/mask/paste layers were replaced")
            drill = _children(pad, "drill")
            if original.drill_x_mm is None:
                if drill:
                    raise ValueError("SMD pad gained an undeclared drill")
            else:
                if len(drill) != 1:
                    raise ValueError("Through-hole pad lost its real drill")
                dimensions = drill[0][1:]
                if dimensions and dimensions[0] == "oval":
                    dimensions = dimensions[1:]
                _close(_number(dimensions[0]), original.drill_x_mm, f"{reference}.{number} drill X")
                _close(_number(dimensions[1]) if len(dimensions) > 1 and not isinstance(dimensions[1], list) else _number(dimensions[0]),
                       original.drill_y_mm, f"{reference}.{number} drill Y")
        # Count actual upstream drawing items; UUID replacement and reference
        # text placement must not become a generic fabricated outline.
        for kind in ("fp_line", "fp_rect", "fp_circle", "fp_arc", "fp_poly"):
            if len(_children(footprint, kind)) != len(_children(item.footprint_tree, kind)):
                raise ValueError("Real footprint graphical geometry was dropped or invented")
        records.append({"reference": reference, "footprint": item.footprint_id,
                        "pad_count": len(pads), "rotation_deg": position.rotation,
                        "x_mm": position.x_mm, "y_mm": position.y_mm})
    if _children(tree, "segment") or _children(tree, "arc") or _children(tree, "via"):
        raise ValueError("The unrouted native fixture unexpectedly contains routed copper")
    return {"status": "passed", "method": "bounded-board-AST-vs-pinned-inventory",
            "native": False, "components": records, "routed": False}


def verify_pcbnew_geometry(bound: Any, board_path: Path) -> dict[str, Any]:
    """Verify native absolute pad positions/sizes/drills when pcbnew exists."""
    try:
        import pcbnew
    except ImportError as exc:
        return {"status": "unavailable", "native": False, "reason": f"pcbnew import unavailable ({type(exc).__name__}); bounded geometry and native CLI loading remain separate evidence"}
    board = pcbnew.LoadBoard(str(board_path))
    if board is None:
        raise ValueError("pcbnew could not load the library-backed board")
    footprints = {item.GetReference(): item for item in board.GetFootprints()}
    expected = {item.component.reference: item for item in bound.components.values()}
    if set(footprints) != set(expected):
        raise ValueError("Native board footprint references differ from the model")
    pads_checked = 0
    for reference, item in sorted(expected.items()):
        footprint = footprints[reference]
        position = item.component.position
        _close(pcbnew.ToMM(footprint.GetPosition().x), position.x_mm, f"native {reference} X")
        _close(pcbnew.ToMM(footprint.GetPosition().y), position.y_mm, f"native {reference} Y")
        _angle(footprint.GetOrientationDegrees(), position.rotation, f"native {reference} rotation")
        if footprint.IsFlipped():
            raise ValueError("Native board footprint was flipped unexpectedly")
        actual_pads = {pad.GetNumber(): pad for pad in footprint.Pads()}
        originals = {pad.number: pad for pad in item.footprint_inventory.pads}
        if set(actual_pads) != set(originals):
            raise ValueError("Native board pad numbers differ from the real library")
        angle = math.radians(position.rotation)
        for number, original in sorted(originals.items()):
            pad = actual_pads[number]
            # KiCad board coordinates grow downwards; a positive placement
            # rotation maps local (x,y) to (x*cos+y*sin,-x*sin+y*cos).
            x = position.x_mm + original.x_mm * math.cos(angle) + original.y_mm * math.sin(angle)
            y = position.y_mm - original.x_mm * math.sin(angle) + original.y_mm * math.cos(angle)
            _close(pcbnew.ToMM(pad.GetPosition().x), x, f"native {reference}.{number} X")
            _close(pcbnew.ToMM(pad.GetPosition().y), y, f"native {reference}.{number} Y")
            _close(pcbnew.ToMM(pad.GetSize().x), original.size_x_mm, f"native {reference}.{number} width")
            _close(pcbnew.ToMM(pad.GetSize().y), original.size_y_mm, f"native {reference}.{number} height")
            _angle(pad.GetOrientationDegrees(), original.rotation + position.rotation, f"native {reference}.{number} rotation")
            _close(pcbnew.ToMM(pad.GetDrillSize().x), original.drill_x_mm or 0, f"native {reference}.{number} drill X")
            _close(pcbnew.ToMM(pad.GetDrillSize().y), original.drill_y_mm or 0, f"native {reference}.{number} drill Y")
            if pad.GetNetname() != item.component.pins.get(number, ""):
                raise ValueError("Native pad net does not match the declared pin mapping")
            pads_checked += 1
    return {"status": "passed", "native": True, "method": "pcbnew-native-absolute-pad-geometry",
            "pads_checked": pads_checked, "fabrication_ready": False}


def verify_native_netlist(bound: Any, path: Path) -> dict[str, Any]:
    """Require exact native component pin nets and actual electrical pin types."""
    root = ET.parse(path).getroot()
    expected = {item.component.reference: item for item in bound.components.values()}
    components = {node.attrib["ref"]: node for node in root.findall("components/comp")}
    if set(components) != set(expected):
        raise ValueError("Native schematic component identities differ from the model")
    actual_nets: dict[str, set[tuple[str, str]]] = {}
    for net in root.findall("nets/net"):
        name = net.attrib["name"]
        if name in actual_nets:
            raise ValueError("Native schematic repeats a global net name")
        actual_nets[name] = {(node.attrib["ref"], node.attrib["pin"]) for node in net.findall("node")}
    expected_nets: dict[str, set[tuple[str, str]]] = {}
    for reference, item in expected.items():
        for number, net in item.component.pins.items():
            expected_nets.setdefault(net, set()).add((reference, number))
        library, name = item.symbol_id.split(":", 1)
        source = components[reference].find("libsource")
        if source is None or (source.get("lib"), source.get("part")) != (library, name):
            raise ValueError("Native schematic did not retain the real symbol identity")
        libparts = [part for part in root.findall("libparts/libpart") if (part.get("lib"), part.get("part")) == (library, name)]
        if len(libparts) != 1:
            raise ValueError("Native netlist is missing the bound symbol pin definition")
        pins = {pin.attrib["num"]: pin for pin in libparts[0].findall("pins/pin")}
        for pin in item.symbol_inventory.pins:
            actual = pins.get(pin.number)
            if actual is None or actual.get("type") != pin.electrical_type or actual.get("name") != pin.name:
                raise ValueError("Native netlist altered a real symbol pin name/type")
    if actual_nets != expected_nets:
        raise ValueError("Native schematic global pin-net connectivity differs from the model")
    return {"status": "passed", "native": True, "references": sorted(expected),
            "nets": {name: [list(pair) for pair in sorted(pins)] for name, pins in sorted(expected_nets.items())},
            "electrical_pin_types_preserved": True}


def run_smoke(output_dir: Path, fixture_dir: Path | None = None) -> dict[str, Any]:
    destination = Path(output_dir).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    fixture = Path(fixture_dir).resolve() if fixture_dir is not None else Path(__file__).resolve().parents[3] / "examples/library_divider"
    backend = KiCadBackend(timeout=180)
    report: dict[str, Any] = {"status": "failed", "fabrication_ready": False, "routed": False,
                              "fixture": "official-pinned-kicad-library-divider", "diagnostic": backend.diagnose()}
    try:
        if not report["diagnostic"]["supported"]:
            raise RuntimeError("Real KiCad 9 or 10 is mandatory; this smoke cannot be mocked or skipped")
        from ..bindings import load_bindings
        from .library_kicad import generate_bound

        design = load_design(fixture / "design.json")
        bound = load_bindings(design, fixture / "bindings.json")
        generated = generate_bound(bound, destination / "source")
        report["provenance"] = bound.provenance
        report["files"] = {name: str(path.relative_to(destination)) for name, path in generated.items()}
        report["geometry"] = verify_board_geometry(bound, generated["board"])
        report["pcbnew_geometry"] = verify_pcbnew_geometry(bound, generated["board"])
        exported = backend.export_netlist(generated["schematic"], destination / "native.net")
        report["native_netlist_export"] = exported
        if exported["status"] != "passed":
            raise RuntimeError("Native KiCad could not load/export the library-backed schematic")
        report["connectivity"] = verify_native_netlist(bound, Path(exported["path"]))
        parity = check_parity(design, Path(exported["path"]), generated["board"])
        report["parity"] = parity.to_dict()
        if parity.blocking:
            raise RuntimeError("Real model/schematic/PCB pin-net parity failed")
        # Deliberately alter one mapping in a copy and prove that actual native
        # evidence rejects the mismatch rather than being treated as a pass.
        changed = type(design).from_dict(design.to_dict())
        changed.root.components[1].pins["1"] = "GND"
        mismatched = check_parity(changed, Path(exported["path"]), generated["board"])
        report["intentional_mismatch"] = mismatched.to_dict()
        if not mismatched.blocking:
            raise RuntimeError("Parity did not reject the intentionally incorrect pin mapping")
        report["exports"] = backend.export(generated["board"], generated["schematic"], destination / "exports", layers=design.board.layers)
        for name in ("gerbers", "drill", "positions", "schematic_svg"):
            if report["exports"]["products"][name]["status"] != "passed":
                raise RuntimeError(f"Native {name} failed to load/export real library geometry")
        checks = backend.check(generated["board"], generated["schematic"], destination / "checks")
        report["checks"] = checks
        erc, drc = checks["erc"], checks["drc"]
        for name, evidence in checks.items():
            if not evidence["executed"] or "native_report" not in evidence:
                raise RuntimeError(f"Native {name} did not produce fresh valid evidence")
        if erc["status"] != "passed":
            raise RuntimeError("Native ERC must pass for the nominal real-library mapping")
        if drc["status"] != "failed" or not drc["native_report"].get("unconnected_items"):
            raise RuntimeError("Unrouted real-library board lost its required blocking native DRC evidence")
        report["status"] = "passed"
        report["claims"] = {"native_erc_passed": True, "native_pin_net_parity_passed": True,
                             "native_board_load_passed": True, "unrouted_drc_blocked": True,
                             "native_coordinate_verification": report["pcbnew_geometry"]["status"] == "passed",
                             "fabrication_ready": False}
        report["artifact_sha256"] = {name: sha256_file(path) for name, path in generated.items()}
    except (RuntimeError, OSError, ValueError, TypeError, KeyError, AttributeError, ET.ParseError) as exc:
        report["reason"] = f"{type(exc).__name__}: {exc}"
    finally:
        (destination / "smoke-report.json").write_text(json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("build/native-libraries"))
    parser.add_argument("--fixture", type=Path)
    args = parser.parse_args()
    result = run_smoke(args.output, args.fixture)
    print(json.dumps({"status": result["status"], "fabrication_ready": False,
                      "reason": result.get("reason", "Native real-library mapping/geometry loaded; unrouted DRC remains blocked")}, indent=2))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
