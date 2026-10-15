"""Conservative model/schematic-netlist/PCB identity checks.

This compares declared references, values, footprint identifiers and pin nets. It
does not certify physical pad geometry, a datasheet pinout, routing, or DFM.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

from .artifacts import sha256_file
from .model import Design
from .reports import CheckReport, Issue


MAX_INPUT_BYTES = 32 * 1024 * 1024


class ParityError(ValueError):
    pass


def _read(path: Path) -> str:
    if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= MAX_INPUT_BYTES:
        raise ParityError(f"Missing, linked, empty or oversized input: {path.name}")
    return path.read_text(encoding="utf-8-sig")


def parse_sexpr(text: str) -> list:
    """Bounded data parser for KiCad expressions. Nothing is evaluated."""
    stack, roots = [], []
    index, tokens = 0, 0
    while index < len(text):
        char = text[index]
        if char.isspace():
            index += 1
            continue
        if char == ";":
            end = text.find("\n", index)
            index = len(text) if end == -1 else end + 1
            continue
        tokens += 1
        if tokens > 1_000_000:
            raise ParityError("KiCad file contains too many tokens")
        if char == "(":
            item = []
            (stack[-1] if stack else roots).append(item)
            stack.append(item)
            if len(stack) > 128:
                raise ParityError("KiCad nesting exceeds 128 levels")
            index += 1
        elif char == ")":
            if not stack:
                raise ParityError("Unbalanced KiCad parentheses")
            stack.pop()
            index += 1
        else:
            if not stack:
                raise ParityError("Unexpected token outside KiCad root")
            if char == '"':
                index += 1
                value = []
                while index < len(text) and text[index] != '"':
                    if text[index] == "\\":
                        index += 1
                        if index == len(text):
                            raise ParityError("Truncated KiCad quoted escape")
                        value.append({"n": "\n", "r": "\r", "t": "\t"}.get(text[index], text[index]))
                    else:
                        value.append(text[index])
                    index += 1
                if index == len(text):
                    raise ParityError("Unterminated KiCad quoted string")
                index += 1
                atom = "".join(value)
            else:
                end = index
                while end < len(text) and not text[end].isspace() and text[end] not in "()":
                    end += 1
                atom, index = text[index:end], end
            stack[-1].append(atom)
    if stack or len(roots) != 1 or not isinstance(roots[0], list):
        raise ParityError("KiCad file must contain one balanced root expression")
    return roots[0]


def _children(node: list, name: str) -> list[list]:
    return [item for item in node[1:] if isinstance(item, list) and item and item[0] == name]


def _field(node: list, name: str, index: int = 1, default: str = "") -> str:
    found = _children(node, name)
    if len(found) > 1:
        raise ParityError(f"Duplicate KiCad field: {name}")
    if not found:
        return default
    if len(found[0]) <= index or not isinstance(found[0][index], str):
        raise ParityError(f"Invalid KiCad field: {name}")
    return found[0][index]


def parse_board(path: str | Path) -> dict[str, dict]:
    text = _read(Path(path))
    root = parse_sexpr(text)
    if not root or root[0] != "kicad_pcb":
        raise ParityError("Expected kicad_pcb root")
    net_table = {}
    for net in _children(root, "net"):
        if (len(net) != 3 or not all(isinstance(value, str) for value in net)
                or not net[1].isascii() or not net[1].isdecimal()):
            raise ParityError("Invalid PCB net declaration")
        code, name = int(net[1]), net[2]
        if code in net_table or name in net_table.values() or (code == 0 and name) or (code > 0 and not name):
            raise ParityError("Duplicate or invalid PCB net code/name")
        net_table[code] = name
    parts = {}
    for footprint in _children(root, "footprint"):
        if len(footprint) < 2 or not isinstance(footprint[1], str):
            raise ParityError("PCB footprint identifier is missing")
        properties = {}
        for item in _children(footprint, "property"):
            if len(item) < 3 or not all(isinstance(v, str) for v in item[:3]):
                raise ParityError("Invalid PCB footprint property")
            if item[1] in properties:
                raise ParityError("Duplicate footprint property")
            properties[item[1]] = item[2]
        for item in _children(footprint, "fp_text"):
            if len(item) >= 3:
                key = {"reference": "Reference", "value": "Value"}.get(item[1])
                if key:
                    if key in properties and properties[key] != item[2]:
                        raise ParityError(f"Conflicting PCB {key} fields")
                    properties[key] = item[2]
        ref = properties.get("Reference", "")
        if not ref or ref in parts:
            raise ParityError(f"Missing or duplicate PCB reference: {ref!r}")
        pins = {}
        for pad in _children(footprint, "pad"):
            if len(pad) < 2 or not isinstance(pad[1], str):
                raise ParityError("Invalid PCB pad number")
            declarations = _children(pad, "net")
            if len(declarations) > 1:
                raise ParityError("Duplicate pad net declaration")
            net = _field(pad, "net", 2)
            if declarations:
                code = _field(pad, "net", 1)
                if not code.isascii() or not code.isdecimal() or net_table.get(int(code)) != net:
                    raise ParityError(f"{ref}.{pad[1]}: pad net code/name does not match board net table")
            if not net:
                continue
            if pad[1] in pins and pins[pad[1]] != net:
                raise ParityError(f"{ref}: repeated pad has conflicting nets")
            pins[pad[1]] = net
        parts[ref] = {"value": properties.get("Value", ""), "footprint": footprint[1], "pins": pins}
    return parts


def parse_netlist(path: str | Path) -> dict[str, dict]:
    text = _read(Path(path))
    if "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
        raise ParityError("XML declarations/entities are not allowed")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise ParityError(f"Invalid KiCad XML netlist: {exc}") from exc
    if root.tag != "export" or len(root.findall("components")) != 1 or len(root.findall("nets")) != 1:
        raise ParityError("Expected a KiCad XML export with components and nets")
    parts = {}
    for item in root.findall("components/comp"):
        ref = item.get("ref")
        if not ref or ref in parts:
            raise ParityError("Missing or duplicate schematic component reference")
        if len(item.findall("value")) != 1 or len(item.findall("footprint")) != 1:
            raise ParityError("Schematic components require one value and one footprint field")
        parts[ref] = {"value": item.findtext("value") or "", "footprint": item.findtext("footprint") or "",
                      "pins": {}}
    names = set()
    for net in root.findall("nets/net"):
        name = net.get("name")
        if not name or name in names:
            raise ParityError("Missing or duplicate schematic net name")
        names.add(name)
        for node in net.findall("node"):
            ref, pin = node.get("ref"), node.get("pin")
            if ref not in parts or not pin:
                raise ParityError("Schematic net references an unknown component/pin")
            if pin in parts[ref]["pins"]:
                raise ParityError(f"{ref}.{pin}: duplicate netlist node")
            parts[ref]["pins"][pin] = name
    return parts


def check_parity(design: Design, netlist: str | Path, board: str | Path) -> CheckReport:
    report = CheckReport("model-parity", metadata={"completed": False, "native": False,
        "design_hash": design.fingerprint(), "scope": "declared-reference-value-footprint-pin-nets",
        "physical_footprints_qualified": False})
    expected = {component.reference: {"value": component.value, "footprint": component.footprint,
                                     "pins": dict(component.pins)}
                for _, block in design.walk_blocks() for component in block.components}
    try:
        actuals = {"schematic": parse_netlist(netlist), "pcb": parse_board(board)}
        report.metadata.update(netlist_sha256=sha256_file(Path(netlist)), board_sha256=sha256_file(Path(board)))
        for source, actual in actuals.items():
            for ref in sorted(expected.keys() - actual.keys()):
                report.add(Issue("parity.missing_component", "error", f"{source}: missing {ref}", ref))
            for ref in sorted(actual.keys() - expected.keys()):
                report.add(Issue("parity.extra_component", "error", f"{source}: undeclared {ref}", ref))
            for ref in sorted(expected.keys() & actual.keys()):
                for field in ("value", "footprint", "pins"):
                    if actual[ref][field] != expected[ref][field]:
                        report.add(Issue(f"parity.{field}_mismatch", "error",
                            f"{source}: {ref} {field} differs from the model", ref,
                            {"expected": expected[ref][field], "actual": actual[ref][field]}))
        if "PCBDEV_PREVIEW_ONLY" in _read(Path(board)):
            report.add(Issue("parity.preview_only", "blocker",
                "Generic preview pads must be replaced with qualified library footprints"))
        report.metadata["completed"] = True
    except (ValueError, OSError, TypeError, UnicodeError) as exc:
        report.add(Issue("parity.invalid_input", "blocker", str(exc)))
    return report
