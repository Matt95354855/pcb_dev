"""Generate an unrouted KiCad draft from pinned, resolved library bindings.

Actual symbols, electrical pin types and two-dimensional footprint geometry are
retained. No generic fallback, automatic routing or manufacturing qualification
is performed. Project-local library tables prevent installed-library drift.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
import math
from pathlib import Path
import re
from typing import TYPE_CHECKING
import uuid
import xml.etree.ElementTree as ET

from ..model import Design, walk_components
from ..sexpr import Quoted, dumps

if TYPE_CHECKING:
    from ..bindings import BoundComponent, BoundDesign


LIBRARY_DRAFT_MARKER = "PCBDEV_LIBRARY_DRAFT"
_NAMESPACE = uuid.UUID("e094364d-799f-5fd4-a88d-0d3e603c2245")


class LibraryGenerationError(ValueError):
    """A bound library draft cannot be generated without unsupported changes."""


def _uid(value: str) -> Quoted:
    return Quoted(str(uuid.uuid5(_NAMESPACE, value)))


def _n(value) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise LibraryGenerationError("KiCad coordinates must be finite numbers") from exc
    if isinstance(value, bool) or not math.isfinite(number):
        raise LibraryGenerationError("KiCad coordinates must be finite numbers")
    return format(number, ".12g") if number else "0"


def _children(node: list, key: str) -> list[list]:
    return [item for item in node[1:] if isinstance(item, list) and item and item[0] == key]


def _one(node: list, key: str, required: bool = False) -> list | None:
    fields = _children(node, key)
    if len(fields) > 1 or (required and not fields):
        raise LibraryGenerationError(f"Expected {'one' if required else 'at most one'} {key} field")
    return fields[0] if fields else None


def _remove(node: list, *keys: str) -> None:
    node[:] = [item for item in node if not (isinstance(item, list) and item and item[0] in keys)]


def _set(node: list, field: list) -> None:
    _remove(node, field[0])
    node.append(field)


def _position(node: list) -> tuple[float, float, float]:
    field = _one(node, "at", required=True)
    if not 3 <= len(field) <= 4:
        raise LibraryGenerationError("Position requires two coordinates and optional angle")
    return float(_n(field[1])), float(_n(field[2])), float(_n(field[3])) if len(field) == 4 else 0.0


def _effects(hidden: bool = False) -> list:
    result = ["effects", ["font", ["size", "1.27", "1.27"]]]
    if hidden:
        result.append(["hide", "yes"])
    return result


def _property(name: str, value: str, *, x: float = 0, y: float = 0,
              layer: str | None = None, hidden: bool = False) -> list:
    result = ["property", Quoted(name), Quoted(value), ["at", _n(x), _n(y), "0"]]
    if layer:
        result.append(["layer", Quoted(layer)])
        if hidden:
            result.append(["hide", "yes"])
        result.append(_effects())
    else:
        result.append(_effects(hidden))
    return result


def _parts(identifier: str) -> tuple[str, str]:
    if not isinstance(identifier, str) or identifier.count(":") != 1:
        raise LibraryGenerationError("Library identifiers require namespace:item")
    namespace, name = identifier.split(":")
    if (not namespace or not name or name in {".", ".."} or name.endswith((".", " "))
            or any(char in identifier for char in ("/", "\\", "\0"))
            or any(ord(char) < 32 for char in identifier)
            or name.split(".", 1)[0].upper() in {"CON", "PRN", "AUX", "NUL", *{f"COM{i}" for i in range(1, 10)}, *{f"LPT{i}" for i in range(1, 10)}}):
        raise LibraryGenerationError("Unsafe or unsupported library item identifier")
    return namespace, name


def _pins(binding: BoundComponent) -> dict:
    pins = {}
    for pin in binding.symbol_inventory.pins:
        if pin.unit not in (0, 1) or pin.convert not in (0, 1):
            raise LibraryGenerationError("Only one-unit symbols with common/normal conversion are supported")
        identity = (pin.name, pin.electrical_type, pin.shape, pin.x_mm, pin.y_mm,
                    pin.rotation % 360, pin.length_mm, pin.hidden)
        if pin.number in pins:
            previous = pins[pin.number]
            previous_identity = (previous.name, previous.electrical_type, previous.shape,
                                 previous.x_mm, previous.y_mm, previous.rotation % 360,
                                 previous.length_mm, previous.hidden)
            if identity != previous_identity:
                raise LibraryGenerationError("Conflicting duplicate symbol pin positions or types")
        else:
            pins[pin.number] = pin
    if not pins:
        raise LibraryGenerationError("A physical bound symbol must contain electrical pins")
    if (set(binding.component.pins) & set(binding.no_connect_pins)
            or set(binding.component.pins) | set(binding.no_connect_pins) != set(pins)):
        raise LibraryGenerationError("Every library pin requires one net mapping or explicit no-connect")
    pad_numbers = {pad.number for pad in binding.footprint_inventory.pads if pad.number and pad.type != "np_thru_hole"}
    if pad_numbers != set(pins):
        raise LibraryGenerationError("Actual footprint pad numbers must match actual symbol pins")
    return pins


def _validated(bound: BoundDesign) -> tuple[Design, list]:
    from ..bindings import BoundDesign

    if not isinstance(bound, BoundDesign):
        raise LibraryGenerationError("generate_bound requires a validated BoundDesign")
    design = Design.from_dict(bound.design.to_dict())
    expected = dict(walk_components(design))
    if set(expected) != set(bound.components):
        raise LibraryGenerationError("Bindings must cover every model component exactly")
    items = []
    for path, binding in sorted(bound.components.items()):
        component = expected[path]
        if binding.path != path or asdict(binding.component) != asdict(component):
            raise LibraryGenerationError("Binding component no longer matches the canonical design")
        if binding.unit != 1 or (component.position and component.position.side != "top"):
            raise LibraryGenerationError("Only top-side placement and symbol unit one are supported")
        if binding.footprint_id != component.footprint:
            raise LibraryGenerationError("Bound physical footprint identity differs from the model")
        _parts(binding.symbol_id)
        _parts(binding.footprint_id)
        if (not isinstance(binding.symbol_tree, list) or binding.symbol_tree[:1] != ["symbol"]
                or binding.symbol_tree[1] != binding.symbol_id
                or not isinstance(binding.footprint_tree, list)
                or binding.footprint_tree[:1] != ["footprint"]):
            raise LibraryGenerationError("Bindings contain unsupported source trees")
        if _one(binding.symbol_tree, "extends") is not None:
            raise LibraryGenerationError("Symbol inheritance must be resolved before generation")
        layer = _one(binding.footprint_tree, "layer", required=True)
        if layer[1:] != ["F.Cu"]:
            raise LibraryGenerationError("Source footprint must be on the top copper layer")
        if _one(binding.footprint_tree, "at") and any(_position(binding.footprint_tree)):
            raise LibraryGenerationError("Source footprint must be an unplaced local library definition")
        items.append((path, binding, _pins(binding)))
    return design, items


def _library_files(items: list) -> tuple[dict, list, list]:
    symbols, footprints = {}, {}
    for _, binding, _ in items:
        symbol = deepcopy(binding.symbol_tree)
        namespace, shortname = _parts(binding.symbol_id)
        symbol[1] = Quoted(shortname)
        previous = symbols.setdefault(namespace, {}).setdefault(shortname, symbol)
        if dumps(previous) != dumps(symbol):
            raise LibraryGenerationError("One symbol identity refers to different source definitions")
        namespace, shortname = _parts(binding.footprint_id)
        footprint = deepcopy(binding.footprint_tree)
        footprint[1] = Quoted(shortname)
        # Active local libraries must match the safe 2D board derivative. The
        # publisher retains captured original bytes and attribution separately.
        _remove(footprint, "model")
        previous = footprints.setdefault(namespace, {}).setdefault(shortname, footprint)
        if dumps(previous) != dumps(footprint):
            raise LibraryGenerationError("One footprint identity refers to different source definitions")
    content = {}
    symbol_entries, footprint_entries = [], []
    for namespace, definitions in sorted(symbols.items()):
        digest = hashlib.sha256(namespace.encode()).hexdigest()[:16]
        relative = f"libraries/symbols/sym_{digest}.kicad_sym"
        root = ["kicad_symbol_lib", ["version", "20241209"], ["generator", Quoted("pcb_dev")],
                ["generator_version", Quoted("9.0")], *[definitions[name] for name in sorted(definitions)]]
        content["symbol_library_" + digest] = (relative, dumps(root) + "\n")
        symbol_entries.append(["lib", ["name", Quoted(namespace)], ["type", Quoted("KiCad")],
                               ["uri", Quoted("${KIPRJMOD}/" + relative)], ["options", Quoted("")],
                               ["descr", Quoted("Pinned PCBDev source snapshot; library draft")]])
    for namespace, definitions in sorted(footprints.items()):
        digest = hashlib.sha256(namespace.encode()).hexdigest()[:16]
        directory = f"libraries/footprints/fp_{digest}.pretty"
        for name, tree in sorted(definitions.items()):
            identifier = hashlib.sha256((namespace + ":" + name).encode()).hexdigest()[:16]
            content["footprint_library_" + identifier] = (directory + "/" + name + ".kicad_mod", dumps(tree) + "\n")
        footprint_entries.append(["lib", ["name", Quoted(namespace)], ["type", Quoted("KiCad")],
                                  ["uri", Quoted("${KIPRJMOD}/" + directory)], ["options", Quoted("")],
                                  ["descr", Quoted("Pinned PCBDev source snapshot; library draft")]])
    return content, symbol_entries, footprint_entries


def _schematic(name: str, stem: str, items: list) -> list:
    root_uuid = _uid(name + ":schematic")
    definitions = {}
    for _, binding, _ in items:
        tree = deepcopy(binding.symbol_tree)
        previous = definitions.setdefault(binding.symbol_id, tree)
        if dumps(previous) != dumps(tree):
            raise LibraryGenerationError("Embedded symbol identity has conflicting source definitions")
    root = ["kicad_sch", ["version", "20250114"], ["generator", Quoted("pcb_dev")],
            ["generator_version", Quoted("9.0")], ["uuid", root_uuid], ["paper", Quoted("A3")],
            ["title_block", ["title", Quoted(name)],
             ["comment", "1", Quoted(LIBRARY_DRAFT_MARKER + ": real pinned libraries; unrouted and unqualified")]],
            ["lib_symbols", *[definitions[key] for key in sorted(definitions)]]]
    y = 30.0
    for path, binding, pins in items:
        component = binding.component
        # Logical symbols stay at zero rotation; physical placement is independent.
        x = 70.0
        instance = ["symbol", ["lib_id", Quoted(binding.symbol_id)], ["at", _n(x), _n(y), "0"],
                    ["unit", "1"], ["exclude_from_sim", "no"], ["in_bom", "yes"], ["on_board", "yes"],
                    ["dnp", "no" if component.fitted else "yes"], ["uuid", _uid(path + ":symbol")]]
        properties = {}
        for source in _children(binding.symbol_tree, "property"):
            if len(source) < 3 or not isinstance(source[1], str):
                raise LibraryGenerationError("Invalid source symbol property")
            key = str(source[1])
            if key.startswith("ki_"):
                continue
            field = deepcopy(source)
            if _one(field, "at"):
                px, py, angle = _position(field)
                _set(field, ["at", _n(x + px), _n(y - py), _n(angle)])
            else:
                field.append(["at", _n(x), _n(y), "0"])
            properties[key] = field
        for key, value, hidden in (("Reference", component.reference, False), ("Value", component.value, False),
                                   ("Footprint", binding.footprint_id, True), ("Datasheet", "", True)):
            if key not in properties:
                properties[key] = _property(key, value, x=x + 7.62, y=y, hidden=hidden)
            elif key != "Datasheet":
                properties[key][2] = Quoted(value)
        properties["PCBDevStatus"] = _property("PCBDevStatus", LIBRARY_DRAFT_MARKER, x=x, y=y, hidden=True)
        instance.extend(properties[key] for key in sorted(properties))
        for number in sorted(pins):
            instance.append(["pin", Quoted(number), ["uuid", _uid(path + ":pin:" + number)]])
        instance.append(["instances", ["project", Quoted(stem),
                                      ["path", Quoted("/" + root_uuid), ["reference", Quoted(component.reference)], ["unit", "1"]]]])
        root.append(instance)
        for number, pin in sorted(pins.items()):
            px, py = x + pin.x_mm, y - pin.y_mm
            if number in binding.no_connect_pins:
                root.append(["no_connect", ["at", _n(px), _n(py)], ["uuid", _uid(path + ":nc:" + number)]])
            else:
                net = component.pins[number]
                root.append(["global_label", Quoted(net), ["shape", "passive"], ["at", _n(px), _n(py), "0"],
                             ["effects", ["font", ["size", "1", "1"]], ["justify", "right"]],
                             ["uuid", _uid(path + ":label:" + number)],
                             ["property", Quoted("Intersheetrefs"), Quoted("${INTERSHEET_REFS}"),
                              ["at", _n(px), _n(py), "0"], ["effects", ["font", ["size", "1", "1"]], ["hide", "yes"]]]])
        span = max(pin.y_mm for pin in pins.values()) - min(pin.y_mm for pin in pins.values())
        y += max(20.32, span + 15.24)
    return root


def _identities(tree: list, path: str) -> None:
    """Give each cloned board object a stable identity, remapping group members."""
    replacements = {}
    drawable = {"pad", "fp_line", "fp_arc", "fp_rect", "fp_poly", "fp_circle", "fp_curve", "fp_text", "fp_text_box", "group", "image"}

    def visit(node: list, address: str):
        existing = _children(node, "uuid") + _children(node, "tstamp")
        if len(existing) > 1:
            raise LibraryGenerationError("Duplicate source object UUID")
        if existing:
            if len(existing[0]) != 2 or not isinstance(existing[0][1], str):
                raise LibraryGenerationError("Malformed source object UUID")
            old = str(existing[0][1])
            new = _uid(path + ":object:" + address)
            replacements[old] = new
            _remove(node, "uuid", "tstamp")
            node.append(["uuid", new])
        elif node and (node[0] in drawable or (node[0] == "property" and _one(node, "at"))):
            node.append(["uuid", _uid(path + ":object:" + address)])
        for index, child in enumerate(node):
            if isinstance(child, list) and child and child[0] not in {"uuid", "tstamp"}:
                visit(child, address + "/" + str(index))

    visit(tree, "root")

    def members(node):
        if node and node[0] == "members":
            node[1:] = [replacements.get(str(atom), atom) if isinstance(atom, str) else atom for atom in node[1:]]
        for child in node:
            if isinstance(child, list):
                members(child)

    members(tree)


def _footprint(path: str, binding: BoundComponent, pins: dict, net_codes: dict,
               name: str, index: int) -> list:
    component = binding.component
    position = component.position
    x = position.x_mm if position else 10 + index % 5 * 12
    y = position.y_mm if position else 10 + index // 5 * 15
    rotation = position.rotation if position else 0
    footprint = deepcopy(binding.footprint_tree)
    footprint[1] = Quoted(binding.footprint_id)
    _remove(footprint, "version", "generator", "generator_version", "at", "uuid", "tstamp", "path", "sheetfile", "sheetname", "model")
    footprint.extend([["at", _n(x), _n(y), _n(rotation)], ["uuid", _uid(path + ":footprint")],
                      ["path", Quoted("/" + _uid(name + ":schematic") + "/" + _uid(path + ":symbol"))]])
    attr = _one(footprint, "attr")
    if attr is None:
        attr = ["attr"]
        footprint.append(attr)
    attr[:] = [atom for atom in attr if atom != "dnp"]
    if not component.fitted:
        attr.append("dnp")
    properties = {}
    for field in _children(footprint, "property"):
        if len(field) >= 3:
            properties[str(field[1])] = field
    for key, value in (("Reference", component.reference), ("Value", component.value)):
        if key in properties:
            properties[key][2] = Quoted(value)
        else:
            field = _property(key, value, y=-2 if key == "Reference" else 2,
                              layer="F.SilkS" if key == "Reference" else "F.Fab")
            footprint.append(field)
    footprint.append(_property("PCBDevStatus", LIBRARY_DRAFT_MARKER, layer="F.Fab", hidden=True))
    for field in footprint[2:]:
        if not isinstance(field, list) or not field:
            continue
        # Pad positions and angles stay exactly in the source footprint's local
        # frame. Native KiCad adds the parent footprint rotation on load; adding
        # it here would rotate pads twice (confirmed by native 9/10 geometry).
        if field[0] in {"property", "fp_text"} and _one(field, "at"):
            px, py, angle = _position(field)
            _set(field, ["at", _n(px), _n(py), _n(angle + rotation)])
        if field[0] == "fp_text" and len(field) >= 3:
            if field[1] == "reference":
                field[2] = Quoted(component.reference)
            elif field[1] == "value":
                field[2] = Quoted(component.value)
        # Text boxes, like pads, store a local angle. Ordinary property/fp_text
        # fields above use an absolute file angle in KiCad's separate parser.
        if field[0] == "pad":
            number = str(field[1])
            _remove(field, "net", "pinfunction", "pintype")
            if field[2] != "np_thru_hole" and number and number in component.pins:
                net = component.pins[number]
                field.extend([["net", str(net_codes[net]), Quoted(net)],
                              ["pinfunction", Quoted(pins[number].name)],
                              ["pintype", Quoted(pins[number].electrical_type)]])
    _identities(footprint, path)
    # The root identity is independent of source-list ordering and is used by
    # external evidence; child identities are stable per cloned source object.
    _set(footprint, ["uuid", _uid(path + ":footprint")])
    return footprint


def _board(design: Design, items: list, net_codes: dict) -> list:
    layers = [["0", Quoted("F.Cu"), "signal"]]
    if design.board.layers == 4:
        layers += [["4", Quoted("In1.Cu"), "signal"], ["6", Quoted("In2.Cu"), "signal"]]
    layers += [["2", Quoted("B.Cu"), "signal"], ["15", Quoted("B.Paste"), "user"], ["13", Quoted("F.Paste"), "user"],
               ["7", Quoted("B.SilkS"), "user", Quoted("B.Silkscreen")], ["5", Quoted("F.SilkS"), "user", Quoted("F.Silkscreen")],
               ["3", Quoted("B.Mask"), "user"], ["1", Quoted("F.Mask"), "user"], ["25", Quoted("Edge.Cuts"), "user"],
               ["29", Quoted("B.CrtYd"), "user", Quoted("B.Courtyard")], ["31", Quoted("F.CrtYd"), "user", Quoted("F.Courtyard")],
               ["33", Quoted("B.Fab"), "user"], ["35", Quoted("F.Fab"), "user"]]
    root = ["kicad_pcb", ["version", "20241229"], ["generator", Quoted("pcb_dev")], ["generator_version", Quoted("9.0")],
            ["general", ["thickness", "1.6"]], ["paper", Quoted("A4")],
            ["title_block", ["title", Quoted(design.name)], ["comment", "1", Quoted(LIBRARY_DRAFT_MARKER + ": pinned actual geometry; unrouted and unqualified")]],
            ["layers", *layers], ["setup", ["pad_to_mask_clearance", "0"]], ["net", "0", Quoted("")]]
    root.extend(["net", str(code), Quoted(net)] for net, code in net_codes.items())
    root.append(["gr_rect", ["start", "0", "0"], ["end", _n(design.board.width_mm), _n(design.board.height_mm)],
                 ["stroke", ["width", "0.05"], ["type", "solid"]], ["fill", "none"], ["layer", Quoted("Edge.Cuts")],
                 ["uuid", _uid(design.name + ":outline")]])
    root.extend(_footprint(path, binding, pins, net_codes, design.name, index) for index, (path, binding, pins) in enumerate(items))
    return root


def _netlist(design: Design, stem: str, items: list, net_codes: dict) -> str:
    root = ET.Element("export", version="E")
    document = ET.SubElement(root, "design")
    ET.SubElement(document, "source").text = stem + ".kicad_sch"
    ET.SubElement(document, "tool").text = "pcb_dev model-derived library draft; native export remains required"
    components = ET.SubElement(root, "components")
    for path, binding, _ in items:
        component = binding.component
        node = ET.SubElement(components, "comp", ref=component.reference)
        ET.SubElement(node, "value").text = component.value
        ET.SubElement(node, "footprint").text = binding.footprint_id
        namespace, shortname = _parts(binding.symbol_id)
        ET.SubElement(node, "libsource", lib=namespace, part=shortname)
        ET.SubElement(node, "sheetpath", names="/", tstamps="/" + _uid(design.name + ":schematic") + "/")
        ET.SubElement(node, "tstamps").text = _uid(path + ":symbol")
    parts = ET.SubElement(root, "libparts")
    definitions = {binding.symbol_id: pins for _, binding, pins in items}
    for identifier, pins in sorted(definitions.items()):
        namespace, shortname = _parts(identifier)
        part = ET.SubElement(parts, "libpart", lib=namespace, part=shortname)
        pinlist = ET.SubElement(part, "pins")
        for number, pin in sorted(pins.items()):
            ET.SubElement(pinlist, "pin", num=number, name=pin.name, type=pin.electrical_type)
    ET.SubElement(root, "libraries")
    nets = ET.SubElement(root, "nets")
    for net, code in net_codes.items():
        netnode = ET.SubElement(nets, "net", code=str(code), name=net)
        for _, binding, pins in items:
            for number, mapped in sorted(binding.component.pins.items()):
                if mapped == net:
                    ET.SubElement(netnode, "node", ref=binding.component.reference, pin=number,
                                  pintype=pins[number].electrical_type, pinfunction=pins[number].name)
    ET.indent(root, space="  ")
    return ET.tostring(root, encoding="unicode", xml_declaration=True) + "\n"


def generate_bound(bound: BoundDesign, output_dir: Path) -> dict[str, Path]:
    """Generate a library draft with local, pinned two-dimensional definitions.

    Caller publishes this directory atomically and copies captured original
    sources/licenses. Additional returned paths cover portable local libraries.
    """
    design, items = _validated(bound)
    name = design.name
    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_")[:80] or "pcb_dev"
    net_codes = {net: index for index, net in enumerate(sorted({net for _, binding, _ in items for net in binding.component.pins.values()}), 1)}
    content, symbols, footprints = _library_files(items)
    content.update({
        "board": (stem + ".kicad_pcb", dumps(_board(design, items, net_codes)) + "\n"),
        "schematic": (stem + ".kicad_sch", dumps(_schematic(name, stem, items)) + "\n"),
        "netlist": (stem + ".net", _netlist(design, stem, items, net_codes)),
        "symbol_table": ("sym-lib-table", dumps(["sym_lib_table", ["version", "7"], *symbols]) + "\n"),
        "footprint_table": ("fp-lib-table", dumps(["fp_lib_table", ["version", "7"], *footprints]) + "\n"),
        "project": (stem + ".kicad_pro", json.dumps({"meta": {"version": 1, "filename": stem + ".kicad_pro"}, "board": {"design_settings": {"rules": {"min_clearance": 0.2, "min_track_width": 0.25, "min_via_diameter": 0.6, "min_through_hole_diameter": 0.3}}}}, sort_keys=True, indent=2) + "\n"),
        "rules": (stem + ".kicad_dru", '(version 1)\n(rule "PCBDev illustrative minimums - verify manufacturer stackup"\n  (constraint clearance (min 0.2mm))\n  (constraint track_width (min 0.25mm)))\n'),
    })
    manifest = {"schema_version": 1, "generator": "pcb_dev", "kind": "library-draft",
                "marker": LIBRARY_DRAFT_MARKER, "fabrication_ready": False, "routed": False, "qualified": False,
                "design_sha256": design.fingerprint(), "provenance": deepcopy(bound.provenance),
                "limitations": ["No copper routes or manufacturing qualification", "Library identity and geometry do not verify a manufacturer datasheet or MPN", "Native ERC/DRC were not executed by generation", "Rules are illustrative and not manufacturer qualification", "External 3D model references are removed from placed footprints and active project libraries; captured originals retain the references as inert source data", "Explicit no-connect markers are emitted; native placeholder-net parity still requires binding-aware review"],
                "components": [{"path": path, "reference": binding.component.reference, "symbol_id": binding.symbol_id,
                                "footprint_id": binding.footprint_id, "position_explicit": binding.component.position is not None,
                                "no_connect_pins": list(binding.no_connect_pins), "omitted_3d_models": list(binding.footprint_inventory.models)}
                               for path, binding, _ in items],
                "artifacts": {key: {"file": filename, "sha256": hashlib.sha256(text.encode()).hexdigest()}
                              for key, (filename, text) in sorted(content.items())}}
    content["manifest"] = ("generation-manifest.json", json.dumps(manifest, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False) + "\n")
    destination = Path(output_dir)
    if destination.is_symlink():
        raise LibraryGenerationError("Output directory must not be a symlink")
    # Validate and serialize every artifact before touching the output tree.
    for filename, _ in content.values():
        path = destination / filename
        parents = [destination.joinpath(*Path(filename).parts[:index]) for index in range(len(Path(filename).parts))]
        if path.is_symlink() or any(parent.is_symlink() for parent in parents):
            raise LibraryGenerationError("Output artifact or parent must not be a symlink")
    paths = {}
    for key, (filename, text) in sorted(content.items()):
        path = destination / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
        paths[key] = path
    return paths
