"""Compare pinned pin identities, connected nets and explicit no-connect states.

This opt-in comparison does not change the general parity parser. A native XML
export can prove KiCad interpreted a no-connect marker; invoking this function
alone never proves that KiCad executed, copper is isolated, or a board is ready
for fabrication. Native export evidence and ERC/DRC remain separate requirements.
"""

from __future__ import annotations

from collections import Counter
import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING
import xml.etree.ElementTree as ET

from .model import Design, walk_components
from .parity import ParityError, _children, _field, _read, parse_board, parse_netlist, parse_sexpr
from .reports import CheckReport, Issue
from .artifacts import sha256_file

if TYPE_CHECKING:
    from .bindings import BoundDesign


def _expected(bound: BoundDesign) -> dict:
    from .bindings import BoundDesign
    from .libraries import _footprint, _symbol_library

    if not isinstance(bound, BoundDesign) or not isinstance(bound.design, Design):
        raise ParityError("Expected a loaded BoundDesign")
    design = Design.from_dict(bound.design.to_dict())
    components = dict(walk_components(design))
    if set(components) != set(bound.components):
        raise ParityError("Bindings do not cover exactly the model components")
    if (hashlib.sha256(bound.bindings_content).hexdigest() != bound.provenance.get("bindings_sha256")
            or hashlib.sha256(bound.registry_content).hexdigest() != bound.provenance.get("registry_sha256")):
        raise ParityError("Captured bindings/registry hashes differ from their provenance")
    entries = json.loads(bound.bindings_content)["components"]
    libraries = {item["name"]: item for item in json.loads(bound.registry_content)["libraries"]}
    inventories = {}

    def inventory(alias, kind):
        if alias not in inventories:
            content = bound.source_contents[alias]
            record = libraries[alias]
            if record["kind"] != kind or hashlib.sha256(content).hexdigest() != record["sha256"]:
                raise ParityError("Captured library differs from its registry pin")
            inventories[alias] = (_footprint if kind == "footprint" else _symbol_library)(content, record["file"])
        return inventories[alias]

    result = {}
    for path, binding in bound.components.items():
        component = components[path]
        entry = entries[path]
        if binding.path != path or binding.component != component or binding.footprint_id != component.footprint:
            raise ParityError(f"{path}: binding identity differs from the model")
        symbols = inventory(entry["symbol_library"], "symbols")
        source_symbol = next((symbol for symbol in symbols.symbols if symbol.name == entry["symbol_name"]), None)
        if (binding.symbol_id != entry["symbol_id"] or binding.symbol_name != entry["symbol_name"]
                or binding.symbol_inventory != source_symbol
                or binding.footprint_inventory != inventory(entry["footprint_library"], "footprint")
                or list(binding.no_connect_pins) != sorted(entry["no_connect_pins"])):
            raise ParityError(f"{path}: resolved binding differs from captured pinned sources")
        pins = {}
        for pin in binding.symbol_inventory.pins:
            signature = (pin.name, pin.electrical_type, pin.shape, pin.x_mm, pin.y_mm,
                         pin.rotation % 360, pin.length_mm, pin.hidden)
            if pin.number in pins:
                previous = pins[pin.number]
                before = (previous.name, previous.electrical_type, previous.shape,
                          previous.x_mm, previous.y_mm, previous.rotation % 360,
                          previous.length_mm, previous.hidden)
                if signature != before:
                    raise ParityError(f"{path}: ambiguous library pin {pin.number}")
            pins[pin.number] = pin
        nc = set(binding.no_connect_pins)
        if (not pins or len(nc) != len(binding.no_connect_pins)
                or nc & set(component.pins) or nc | set(component.pins) != set(pins)):
            raise ParityError(f"{path}: incomplete or conflicting connected/no-connect pin declarations")
        pad_numbers = {pad.number for pad in binding.footprint_inventory.pads
                       if pad.number and pad.type != "np_thru_hole"}
        if pad_numbers != set(pins):
            raise ParityError(f"{path}: electrical pads differ from symbol pins")
        result[component.reference] = {"path": path, "binding": binding, "pins": pins,
            "nc": nc, "value": component.value, "footprint": component.footprint,
            "nets": dict(component.pins)}
    return result


def _netlist_details(path: Path, expected: dict) -> tuple[dict, list[dict]]:
    # Retain all validation performed by the unchanged strict general parser.
    parts = parse_netlist(path)
    root = ET.fromstring(_read(path))
    if root.get("version") != "E" or len(root.findall("libparts")) != 1:
        raise ParityError("Bound parity requires KiCad XML version E with one libparts section")
    if set(parts) != set(expected):
        raise ParityError("Native component references differ from the bound model")
    definitions = {}
    for item in root.findall("libparts/libpart"):
        identity = (item.get("lib"), item.get("part"))
        if not all(identity) or identity in definitions or len(item.findall("pins")) != 1:
            raise ParityError("Missing or duplicate native library definition")
        pins = {}
        for pin in item.findall("pins/pin"):
            number = pin.get("num")
            if not number or number in pins or pin.get("name") is None or not pin.get("type"):
                raise ParityError("Invalid or duplicate native library pin")
            pins[number] = (pin.get("name"), pin.get("type"))
        definitions[identity] = pins
    for component in root.findall("components/comp"):
        ref = component.get("ref")
        item = expected[ref]
        sources = component.findall("libsource")
        identity = tuple(item["binding"].symbol_id.split(":", 1))
        if len(sources) != 1 or (sources[0].get("lib"), sources[0].get("part")) != identity:
            raise ParityError(f"{ref}: native symbol identity differs from its binding")
        pins = {number: ("" if pin.name == "~" else pin.name, pin.electrical_type)
                for number, pin in item["pins"].items()}
        if definitions.get(identity) != pins:
            raise ParityError(f"{ref}: native library pin numbers, names or types differ")
        for field in ("value", "footprint"):
            if parts[ref][field] != item[field]:
                raise ParityError(f"{ref}: native {field} differs from the bound model")

    nc_records, observed = [], set()
    declared_nets = {net for item in expected.values() for net in item["nets"].values()}
    for net in root.findall("nets/net"):
        name, nodes = net.get("name"), net.findall("node")
        if not nodes:
            raise ParityError("Native net has no physical pin nodes")
        nc_nodes = []
        for node in nodes:
            ref, number = node.get("ref"), node.get("pin")
            item = expected[ref]
            if number not in item["pins"]:
                raise ParityError(f"{ref}.{number}: undeclared native pin")
            pin = item["pins"][number]
            is_nc = number in item["nc"]
            expected_type = pin.electrical_type + ("+no_connect" if is_nc else "")
            if node.get("pintype") != expected_type:
                raise ParityError(f"{ref}.{number}: native pin type/no-connect annotation differs")
            expected_name = "" if pin.name == "~" else pin.name
            if node.get("pinfunction", "") != expected_name:
                raise ParityError(f"{ref}.{number}: native pin function differs from the library")
            if is_nc:
                nc_nodes.append((ref, number))
            elif item["nets"][number] != name:
                raise ParityError(f"{ref}.{number}: native connected net differs from the model")
            observed.add((ref, number))
        if nc_nodes:
            if len(nc_nodes) != len(nodes) or name in declared_nets:
                raise ParityError("Explicit no-connect shares a native net with a connected pin/net")
            # KiCad 9's exporter annotates singleton nets or stacked pins of
            # the same symbol, at the same endpoint and with the same name.
            # Never accept an arbitrary multi-pin unconnected-* network.
            ref, number = nc_nodes[0]
            first = expected[ref]["pins"][number]
            for other_ref, other_number in nc_nodes[1:]:
                pin = expected[other_ref]["pins"][other_number]
                if other_ref != ref or (pin.x_mm, pin.y_mm, pin.name) != (first.x_mm, first.y_mm, first.name):
                    raise ParityError("Native no-connect network is not a proven stacked-pin group")
            nc_records.append({"native_net": name, "pins": [list(pair) for pair in sorted(nc_nodes)],
                               "kind": "singleton" if len(nc_nodes) == 1 else "stacked"})
    required = {(ref, number) for ref, item in expected.items() for number in item["pins"]}
    if observed != required:
        raise ParityError("Native netlist omits a declared connected or explicit no-connect pin")
    return parts, nc_records


def _board_details(path: Path, expected: dict) -> None:
    parts = parse_board(path)
    if set(parts) != set(expected):
        raise ParityError("PCB component references differ from the bound model")
    text = _read(path)
    if "PCBDEV_PREVIEW_ONLY" in text:
        raise ParityError("Generic preview footprints cannot pass bound parity")
    root = parse_sexpr(text)
    for footprint in _children(root, "footprint"):
        ref = next((field[2] for field in _children(footprint, "property")
                    if len(field) >= 3 and field[1] == "Reference"), "")
        if not ref:
            ref = next((field[2] for field in _children(footprint, "fp_text")
                        if len(field) >= 3 and field[1] == "reference"), "")
        item = expected[ref]
        for field in ("value", "footprint"):
            if parts[ref][field] != item[field]:
                raise ParityError(f"{ref}: PCB {field} differs from the bound model")
        actual_pads = Counter()
        for pad in _children(footprint, "pad"):
            if len(pad) < 4 or not all(isinstance(value, str) for value in pad[:4]):
                raise ParityError(f"{ref}: malformed physical pad")
            number, kind = pad[1:3]
            net = _field(pad, "net", 2)
            if kind == "np_thru_hole" or not number:
                if net:
                    raise ParityError(f"{ref}: mechanical or unnumbered pad has a net")
                continue
            if number not in item["pins"]:
                raise ParityError(f"{ref}.{number}: undeclared electrical pad")
            actual_pads[(number, kind)] += 1
            pin = item["pins"][number]
            pin_type, pin_name = _field(pad, "pintype"), _field(pad, "pinfunction")
            if number in item["nc"]:
                # Initial backend deliberately emits NC pads without a net.
                # Native singleton placeholder nets on PCB are not supported.
                if net:
                    raise ParityError(f"{ref}.{number}: explicit no-connect PCB pad has a net")
                if pin_type and pin_type not in (pin.electrical_type, pin.electrical_type + "+no_connect"):
                    raise ParityError(f"{ref}.{number}: PCB no-connect pin type differs")
                if pin_name and pin_name not in (pin.name, "" if pin.name == "~" else pin.name):
                    raise ParityError(f"{ref}.{number}: PCB no-connect pin function differs")
            elif (net != item["nets"][number] or pin_type != pin.electrical_type
                  or pin_name not in (pin.name, "" if pin.name == "~" else pin.name)):
                raise ParityError(f"{ref}.{number}: PCB connected net/name/type differs")
        required_pads = Counter((pad.number, pad.type) for pad in item["binding"].footprint_inventory.pads
                                if pad.number and pad.type != "np_thru_hole")
        if actual_pads != required_pads:
            raise ParityError(f"{ref}: PCB electrical pad coverage/multiplicity differs from its binding")


def check_bound_parity(bound: BoundDesign, netlist: str | Path, board: str | Path) -> CheckReport:
    """Check explicit bound pin states against XML and PCB pad assignments.

    NC XML nets may have any KiCad-generated name; acceptance depends on exact
    explicit pin identity, actual electrical type + ``+no_connect`` and proven
    singleton/stacked membership, never a net-name prefix. PCB NC pads must have
    no net. Calling this parser does not assert native execution or copper DRC.
    """
    report = CheckReport("model-parity", metadata={"completed": False, "native": False,
        "scope": "bound-reference-value-footprint-pin-nets-and-explicit-nc",
        "physical_footprints_qualified": False, "copper_connectivity_verified": False})
    try:
        expected = _expected(bound)
        report.metadata.update(design_hash=bound.design.fingerprint(),
            bindings_sha256=bound.provenance.get("bindings_sha256"),
            registry_sha256=bound.provenance.get("registry_sha256"))
        paths = Path(netlist), Path(board)
        for path in paths:
            _read(path)  # Bound and reject linked inputs before hashing them.
        report.metadata.update(netlist_sha256=sha256_file(paths[0]), board_sha256=sha256_file(paths[1]))
        _, nc_records = _netlist_details(paths[0], expected)
        _board_details(paths[1], expected)
        if (sha256_file(paths[0]) != report.metadata["netlist_sha256"]
                or sha256_file(paths[1]) != report.metadata["board_sha256"]):
            raise ParityError("Parity inputs changed while being checked")
        report.metadata.update(completed=True, no_connects=nc_records,
            connected_pin_count=sum(len(item["nets"]) for item in expected.values()),
            no_connect_pin_count=sum(len(item["nc"]) for item in expected.values()))
    except (ValueError, OSError, TypeError, UnicodeError, KeyError, AttributeError) as exc:
        report.add(Issue("parity.bound_invalid_input", "blocker", str(exc) or type(exc).__name__))
    return report
