"""Inspect and pin real KiCad footprint and symbol-library data files.

This is an inventory reader and limited geometry review, not a KiCad importer,
IPC-7351 implementation, pad-stack solver, or fabrication qualification.  The
only executed code is this parser; library data cannot contain executable hooks.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import math
from pathlib import Path, PurePosixPath
import re
from typing import Any

from .parity import parse_sexpr
from .reports import CheckReport, Issue
from .transactions import atomic_write_json, read_json


class LibraryError(ValueError):
    """Malformed library data, unresolved inheritance, or an unsafe registry."""


MAX_LIBRARY_BYTES = 32 * 1024 * 1024
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_ALIAS = re.compile(r"^[A-Za-z0-9_-]+$")


def _children(node: list, name: str) -> list[list]:
    return [child for child in node[1:] if type(child) is list and child and child[0] == name]


def _one(node: list, name: str, *, required: bool = False) -> list | None:
    children = _children(node, name)
    if len(children) > 1 or (required and not children):
        raise LibraryError(f"expected {'exactly' if required else 'at most'} one {name!r} field")
    return children[0] if children else None


def _text(value: Any, label: str, *, empty: bool = False) -> str:
    if type(value) is not str or (not empty and not value):
        raise LibraryError(f"{label} must be a {'string' if empty else 'non-empty string'}")
    if any(0xD800 <= ord(char) <= 0xDFFF for char in value):
        raise LibraryError(f"{label} contains an unpaired Unicode surrogate")
    return value


def _numeric(value: Any, label: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise LibraryError(f"{label} must be a finite number") from exc
    if not math.isfinite(result):
        raise LibraryError(f"{label} must be a finite number")
    return result


def _values(node: list, name: str, *, minimum: int, maximum: int | None = None,
            required: bool = True) -> list[str] | None:
    item = _one(node, name, required=required)
    if item is None:
        return None
    values = item[1:]
    maximum = minimum if maximum is None else maximum
    if not minimum <= len(values) <= maximum or any(type(value) is not str for value in values):
        raise LibraryError(f"{name!r} requires {minimum}..{maximum} scalar value(s)")
    return values


def _position(node: list, name: str = "at") -> tuple[float, float, float]:
    values = _values(node, name, minimum=2, maximum=3)
    return (_numeric(values[0], name), _numeric(values[1], name),
            _numeric(values[2], name) if len(values) == 3 else 0)


def _point(node: list, name: str) -> tuple[float, float]:
    values = _values(node, name, minimum=2)
    return _numeric(values[0], name), _numeric(values[1], name)


def _atom(node: list, name: str, *, default: str | None = None) -> str | None:
    values = _values(node, name, minimum=1, required=default is None)
    return values[0] if values else default


def _width(node: list) -> float:
    stroke = _one(node, "stroke")
    direct = _one(node, "width")
    if stroke is not None and direct is not None:
        raise LibraryError("ambiguous legacy width and stroke fields")
    if stroke is not None:
        value = _atom(stroke, "width")
    elif direct is not None:
        if len(direct) != 2 or type(direct[1]) is not str:
            raise LibraryError("invalid silk width")
        value = direct[1]
    else:
        raise LibraryError("silk geometry is missing stroke width")
    width = _numeric(value, "stroke width")
    if width <= 0:
        raise LibraryError("stroke width must be positive")
    return width


@dataclass(slots=True)
class PadInventory:
    number: str
    type: str
    shape: str
    x_mm: float
    y_mm: float
    rotation: float
    size_x_mm: float
    size_y_mm: float
    layers: list[str]
    drill_x_mm: float | None = None
    drill_y_mm: float | None = None
    drill_offset_x_mm: float = 0
    drill_offset_y_mm: float = 0


@dataclass(slots=True)
class SilkSegment:
    layer: str
    start: tuple[float, float]
    end: tuple[float, float]
    width_mm: float


@dataclass(slots=True)
class FootprintInventory:
    name: str
    pads: list[PadInventory]
    courtyard_present: bool
    sha256: str
    source: str = ""
    version: str = ""
    courtyard_layers: list[str] = field(default_factory=list)
    silk_segments: list[SilkSegment] = field(default_factory=list)
    unverified_silk_items: list[str] = field(default_factory=list)
    models: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class SymbolPinInventory:
    number: str
    name: str
    electrical_type: str
    shape: str
    x_mm: float
    y_mm: float
    rotation: float
    length_mm: float
    unit: int
    convert: int
    hidden: bool = False


@dataclass(slots=True)
class SymbolInventory:
    name: str
    pins: list[SymbolPinInventory]
    properties: dict[str, str] = field(default_factory=dict)
    extends: str | None = None
    inheritance_resolved: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class SymbolLibraryInventory:
    symbols: list[SymbolInventory]
    sha256: str
    source: str = ""
    version: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _parse(content: bytes) -> list:
    if not content or len(content) > MAX_LIBRARY_BYTES:
        raise LibraryError(f"library must contain 1..{MAX_LIBRARY_BYTES} bytes")
    try:
        root = parse_sexpr(content.decode("utf-8-sig"))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise LibraryError(f"invalid KiCad library syntax: {exc}") from exc
    return root


def _read(path: str | Path) -> bytes:
    try:
        with Path(path).open("rb") as stream:
            content = stream.read(MAX_LIBRARY_BYTES + 1)
        if not content or len(content) > MAX_LIBRARY_BYTES:
            raise LibraryError(f"library must contain 1..{MAX_LIBRARY_BYTES} bytes")
        return content
    except OSError as exc:
        raise LibraryError(f"cannot read library {path}: {exc}") from exc


def _pad(node: list) -> PadInventory:
    if len(node) < 4:
        raise LibraryError("pad requires number, type and shape")
    number = _text(node[1], "pad number", empty=True)
    pad_type, shape = _text(node[2], "pad type"), _text(node[3], "pad shape")
    x, y, rotation = _position(node)
    size = _values(node, "size", minimum=2)
    layers = _values(node, "layers", minimum=1, maximum=64)
    if any(not layer for layer in layers) or len(layers) != len(set(layers)):
        raise LibraryError("pad layers must be distinct non-empty names")
    drill_x = drill_y = None
    offset_x = offset_y = 0
    drill = _one(node, "drill")
    if drill is not None:
        scalars = [value for value in drill[1:] if type(value) is str]
        if not scalars:
            raise LibraryError("drill size missing")
        if scalars[0] == "oval":
            if len(scalars) != 3:
                raise LibraryError("oval drill requires two dimensions")
            drill_x, drill_y = (_numeric(value, "drill") for value in scalars[1:])
        elif len(scalars) == 1:
            drill_x = drill_y = _numeric(scalars[0], "drill")
        else:
            raise LibraryError("unsupported drill dimensions")
        offset = _values(drill, "offset", minimum=2, required=False)
        if offset:
            offset_x, offset_y = (_numeric(value, "drill offset") for value in offset)
    return PadInventory(number, pad_type, shape, x, y, rotation,
                        _numeric(size[0], "pad size"), _numeric(size[1], "pad size"), layers,
                        drill_x, drill_y, offset_x, offset_y)


def _footprint(content: bytes, source: str) -> FootprintInventory:
    root = _parse(content)
    if len(root) < 2 or root[0] != "footprint":
        raise LibraryError("expected a modern KiCad footprint root")
    name = _text(root[1], "footprint name")
    pads = [_pad(node) for node in _children(root, "pad")]
    courtyard = set()
    silk: list[SilkSegment] = []
    unverified = []
    geometry = {"fp_line", "fp_rect", "fp_poly", "fp_arc", "fp_circle", "fp_curve"}
    for node in root[2:]:
        if type(node) is not list or not node or node[0] not in geometry | {"fp_text", "fp_text_box"}:
            continue
        layer = _atom(node, "layer", default="")
        if layer in {"F.CrtYd", "B.CrtYd"} and node[0] in geometry:
            courtyard.add(layer)
        if layer not in {"F.SilkS", "B.SilkS"}:
            continue
        if node[0] == "fp_line":
            silk.append(SilkSegment(layer, _point(node, "start"), _point(node, "end"), _width(node)))
        elif node[0] == "fp_rect":
            start, end = _point(node, "start"), _point(node, "end")
            points = [start, (end[0], start[1]), end, (start[0], end[1])]
            width = _width(node)
            silk.extend(SilkSegment(layer, point, points[(i + 1) % 4], width) for i, point in enumerate(points))
            if _atom(node, "fill", default="none") != "none":
                unverified.append("filled fp_rect interior")
        elif node[0] == "fp_poly":
            pts = _one(node, "pts", required=True)
            points = []
            for item in pts[1:]:
                if type(item) is not list or len(item) != 3 or item[0] != "xy":
                    raise LibraryError("polygon points require (xy x y)")
                points.append((_numeric(item[1], "polygon x"), _numeric(item[2], "polygon y")))
            if len(points) < 3:
                raise LibraryError("polygon requires at least three points")
            width = _width(node)
            silk.extend(SilkSegment(layer, point, points[(i + 1) % len(points)], width) for i, point in enumerate(points))
            if _atom(node, "fill", default="none") != "none":
                unverified.append("filled fp_poly interior")
        else:
            unverified.append(node[0])
    models = []
    for model in _children(root, "model"):
        if len(model) < 2:
            raise LibraryError("3D model path missing")
        models.append(_text(model[1], "model path"))
    return FootprintInventory(name, pads, bool(courtyard), hashlib.sha256(content).hexdigest(), source,
                              _atom(root, "version", default=""), sorted(courtyard), silk, sorted(unverified), models)


def load_footprint(path: str | Path) -> FootprintInventory:
    """Read the real file's pads, geometry metadata and inert 3D model paths."""
    return _footprint(_read(path), str(Path(path)))


def _symbol_pin(node: list, unit: int, convert: int) -> SymbolPinInventory:
    if len(node) < 3:
        raise LibraryError("symbol pin type and shape missing")
    x, y, rotation = _position(node)
    length = _numeric(_atom(node, "length"), "pin length")
    if length < 0:
        raise LibraryError("symbol pin length cannot be negative")
    name = _one(node, "name", required=True)
    number = _one(node, "number", required=True)
    if len(name) < 2 or len(number) < 2:
        raise LibraryError("pin name or number missing")
    hidden = "hide" in node[3:]
    hide_field = _one(node, "hide")
    if hide_field is not None:
        if hidden or len(hide_field) != 2 or hide_field[1] not in ("yes", "no"):
            raise LibraryError("invalid or duplicate symbol pin hide field")
        hidden = hide_field[1] == "yes"
    return SymbolPinInventory(_text(number[1], "pin number"), _text(name[1], "pin name", empty=True),
                              _text(node[1], "pin electrical type"), _text(node[2], "pin shape"),
                              x, y, rotation, length, unit, convert, hidden)


def _symbol_library(content: bytes, source: str) -> SymbolLibraryInventory:
    root = _parse(content)
    if not root or root[0] != "kicad_symbol_lib":
        raise LibraryError("expected a modern KiCad symbol-library root")
    symbols: dict[str, SymbolInventory] = {}
    for node in _children(root, "symbol"):
        if len(node) < 2:
            raise LibraryError("symbol name missing")
        name = _text(node[1], "symbol name")
        if name in symbols:
            raise LibraryError(f"duplicate symbol {name!r}")
        properties = {}
        for prop in _children(node, "property"):
            if len(prop) < 3:
                raise LibraryError("symbol property name/value missing")
            key, value = _text(prop[1], "property name"), _text(prop[2], "property value", empty=True)
            if key in properties:
                raise LibraryError(f"duplicate symbol property {key!r}")
            properties[key] = value
        extends = _atom(node, "extends", default="") or None
        pins = [_symbol_pin(pin, 0, 0) for pin in _children(node, "pin")]
        for unit_node in _children(node, "symbol"):
            if len(unit_node) < 2 or type(unit_node[1]) is not str:
                raise LibraryError("symbol unit name missing")
            unit_name = unit_node[1].rsplit("_", 2)
            if len(unit_name) != 3 or not all(item.isdigit() for item in unit_name[-2:]):
                raise LibraryError(f"invalid symbol unit name {unit_node[1]!r}")
            unit, convert = int(unit_name[-2]), int(unit_name[-1])
            if _children(unit_node, "symbol"):
                raise LibraryError("nested symbol units are not supported")
            pins.extend(_symbol_pin(pin, unit, convert) for pin in _children(unit_node, "pin"))
        symbols[name] = SymbolInventory(name, pins, properties, extends, extends is None)
    resolved: set[str] = set()
    for start in symbols:
        chain, positions = [], set()
        name = start
        while name not in resolved:
            if name in positions:
                raise LibraryError("symbol inheritance cycle: " + " -> ".join(chain + [name]))
            if name not in symbols:
                raise LibraryError(f"symbol inheritance requires an unavailable parent {name!r}")
            positions.add(name)
            chain.append(name)
            parent = symbols[name].extends
            if parent is None:
                break
            name = parent
        for name in reversed(chain):
            symbol = symbols[name]
            if symbol.extends:
                parent = symbols[symbol.extends]
                if symbol.pins:
                    raise LibraryError(f"derived symbol {name!r} also declares pins; override semantics are unsupported")
                import copy
                symbol.pins = copy.deepcopy(parent.pins)
                symbol.properties = {**parent.properties, **symbol.properties}
            symbol.inheritance_resolved = True
            resolved.add(name)
    return SymbolLibraryInventory(list(symbols.values()), hashlib.sha256(content).hexdigest(), source,
                                  _atom(root, "version", default=""))


def load_symbol_library(path: str | Path) -> SymbolLibraryInventory:
    """Read pin numbers/names/types/unit variants and resolve local inheritance."""
    return _symbol_library(_read(path), str(Path(path)))


def _segment_intersects_rectangle(start: tuple[float, float], end: tuple[float, float],
                                   half_x: float, half_y: float) -> bool:
    lower, upper = 0.0, 1.0
    for origin, delta, half in ((start[0], end[0] - start[0], half_x),
                                (start[1], end[1] - start[1], half_y)):
        if abs(delta) < 1e-15:
            if abs(origin) > half:
                return False
            continue
        left, right = (-half - origin) / delta, (half - origin) / delta
        lower, upper = max(lower, min(left, right)), min(upper, max(left, right))
        if lower > upper:
            return False
    return True


def _silk_overlaps_pad(segment: SilkSegment, pad: PadInventory, clearance_mm: float) -> bool:
    copper = "F.Cu" if segment.layer == "F.SilkS" else "B.Cu"
    if not ({copper, "*.Cu", "F&B.Cu"} & set(pad.layers)):
        return False
    # KiCad's PCB coordinate Y points down while positive angles rotate
    # counter-clockwise on screen. The inverse local transform uses +angle.
    angle = math.radians(pad.rotation)
    cosine, sine = math.cos(angle), math.sin(angle)

    def local(point: tuple[float, float]) -> tuple[float, float]:
        x, y = point[0] - pad.x_mm, point[1] - pad.y_mm
        return x * cosine - y * sine, x * sine + y * cosine

    expansion = segment.width_mm / 2 + clearance_mm
    return _segment_intersects_rectangle(local(segment.start), local(segment.end),
                                         pad.size_x_mm / 2 + expansion, pad.size_y_mm / 2 + expansion)


def check_footprint(footprint: FootprintInventory, *, silk_clearance_mm: float = 0) -> CheckReport:
    """Review positive sizes, pin-1 labels and bounded silk/pad approximation."""
    if type(silk_clearance_mm) not in (int, float) or not math.isfinite(silk_clearance_mm) or silk_clearance_mm < 0:
        raise LibraryError("silk clearance must be a finite nonnegative number")
    report = CheckReport("library-footprint", metadata={
        "completed": True, "native": False, "library_sha256": footprint.sha256,
        "manufacturing_qualified": False, "courtyard_scope": "geometry-presence-only",
        "silk_overlap_scope": "line-rectangle-polygon-strokes-vs-oriented-pad-bounding-rectangles",
        "silk_clearance_mm": silk_clearance_mm, "unverified_silk_items": footprint.unverified_silk_items,
    })
    if not footprint.pads:
        report.add(Issue("footprint.no_pads", "warning", "Footprint has no pads; it may be a mechanical-only item.", footprint.name))
    numbers = {pad.number for pad in footprint.pads if pad.number}
    if numbers and not numbers & {"1", "A1"}:
        report.add(Issue("footprint.pin1_missing", "warning", "No pad labelled 1 or A1 was found; verify the intended pin-1 convention.", footprint.name))
    if not footprint.courtyard_present:
        report.add(Issue("footprint.courtyard_missing", "warning", "No front/back courtyard geometry was found.", footprint.name))
    for index, pad in enumerate(footprint.pads):
        path = f"{footprint.name}.pad[{index}]"
        if pad.size_x_mm <= 0 or pad.size_y_mm <= 0:
            report.add(Issue("footprint.pad_size", "error", "Pad dimensions must be positive.", path))
            continue
        if pad.type in {"thru_hole", "np_thru_hole"}:
            if pad.drill_x_mm is None or pad.drill_y_mm is None or min(pad.drill_x_mm, pad.drill_y_mm) <= 0:
                report.add(Issue("footprint.drill_size", "error", "Through-hole pads require positive drill dimensions.", path))
        if pad.drill_x_mm is not None and pad.drill_y_mm is not None:
            if min(pad.drill_x_mm, pad.drill_y_mm) <= 0:
                report.add(Issue("footprint.drill_size", "error", "Declared drill dimensions must be positive.", path))
            elif pad.type == "thru_hole" and (pad.drill_x_mm / 2 + abs(pad.drill_offset_x_mm) >= pad.size_x_mm / 2
                                               or pad.drill_y_mm / 2 + abs(pad.drill_offset_y_mm) >= pad.size_y_mm / 2):
                report.add(Issue("footprint.drill_exceeds_pad", "error", "Drill leaves no positive bounding-box copper margin; annular geometry requires review.", path))
        if not pad.number and pad.type != "np_thru_hole":
            report.add(Issue("footprint.unnumbered_pad", "warning", "Unnumbered pad cannot be matched to an electrical symbol pin.", path))
        if pad.type not in {"smd", "thru_hole", "np_thru_hole", "connect"}:
            report.add(Issue("footprint.pad_type_unverified", "warning", "Pad type is outside the supported inventory checks.", path))
        if pad.shape not in {"circle", "rect", "oval", "roundrect"}:
            report.add(Issue("footprint.pad_geometry_unverified", "warning", "Pad geometry is outside the silk overlap approximation.", path))
            continue
        overlaps = [i for i, segment in enumerate(footprint.silk_segments)
                    if _silk_overlaps_pad(segment, pad, silk_clearance_mm)]
        if overlaps:
            report.add(Issue("footprint.silk_pad_overlap", "warning", "Silkscreen stroke intersects an expanded pad bounding rectangle; inspect the real geometry.", path,
                             {"pad_number": pad.number, "silk_segment_indices": overlaps}))
    if footprint.unverified_silk_items:
        report.add(Issue("footprint.silk_geometry_unverified", "warning", "Text, arcs, circles, curves or filled interiors were not checked against pads.", footprint.name,
                         {"items": footprint.unverified_silk_items}))
    return report


def check_symbol_footprint(symbol: SymbolInventory, footprint: FootprintInventory) -> CheckReport:
    """Compare physical pin-number coverage; names/types still need datasheets."""
    report = CheckReport("library-pin-map", metadata={"completed": True, "native": False,
                        "footprint_sha256": footprint.sha256, "datasheet_pinout_verified": False})
    pins = {pin.number for pin in symbol.pins}
    pads = {pad.number for pad in footprint.pads if pad.number and pad.type != "np_thru_hole"}
    for number in sorted(pins - pads):
        report.add(Issue("library.pin_without_pad", "error", "Symbol pin has no numbered electrical footprint pad.", f"{symbol.name}.{number}"))
    for number in sorted(pads - pins):
        report.add(Issue("library.pad_without_pin", "error", "Electrical footprint pad has no symbol pin.", f"{footprint.name}.{number}"))
    if not symbol.inheritance_resolved:
        report.add(Issue("library.unresolved_symbol", "blocker", "Symbol inheritance is not resolved.", symbol.name))
    return report


@dataclass(slots=True)
class LibraryPin:
    name: str
    kind: str
    file: str
    sha256: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LibraryPin:
        if type(data) is not dict or set(data) != {"name", "kind", "file", "sha256"}:
            raise LibraryError("library pin requires exactly name, kind, file and sha256")
        if type(data["name"]) is not str or not _ALIAS.fullmatch(data["name"]):
            raise LibraryError("library alias must use ASCII letters, digits, '_' and '-'")
        if data["kind"] not in ("footprint", "symbols"):
            raise LibraryError("library kind must be footprint or symbols")
        if type(data["sha256"]) is not str or not _DIGEST.fullmatch(data["sha256"]):
            raise LibraryError("library SHA-256 pin is invalid")
        file = data["file"]
        if (type(file) is not str or not file or "\\" in file or ":" in file
                or PurePosixPath(file).is_absolute() or any(part in ("", ".", "..") for part in file.split("/"))):
            raise LibraryError("library path must be contained and relative, without traversal")
        return cls(**data)

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(slots=True)
class LibraryRegistry:
    libraries: list[LibraryPin] = field(default_factory=list)
    schema_version: int = 1

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LibraryRegistry:
        if (type(data) is not dict or set(data) != {"schema_version", "libraries"}
                or type(data["schema_version"]) is not int or data["schema_version"] != 1
                or type(data["libraries"]) is not list):
            raise LibraryError("invalid library registry schema")
        libraries = [LibraryPin.from_dict(item) for item in data["libraries"]]
        names = [pin.name for pin in libraries]
        if len(names) != len(set(names)):
            raise LibraryError("duplicate library alias")
        return cls(libraries)

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": self.schema_version,
                "libraries": [pin.to_dict() for pin in sorted(self.libraries, key=lambda pin: pin.name)]}


def load_registry(path: str | Path) -> LibraryRegistry:
    try:
        return LibraryRegistry.from_dict(read_json(path))
    except ValueError as exc:
        raise LibraryError(f"invalid library registry: {exc}") from exc


def register_library(file_path: str | Path, registry_path: str | Path, *, name: str,
                      kind: str | None = None) -> LibraryPin:
    """Pin one local data file after parsing it; never import or download models."""
    source, registry_file = Path(file_path).resolve(), Path(registry_path)
    root = registry_file.parent.resolve()
    if not source.is_relative_to(root):
        raise LibraryError("library file must be inside the registry directory")
    if kind is None:
        kind = {".kicad_mod": "footprint", ".kicad_sym": "symbols"}.get(source.suffix)
    if kind not in ("footprint", "symbols"):
        raise LibraryError("specify footprint or symbols for the library kind")
    content = _read(source)
    (_footprint if kind == "footprint" else _symbol_library)(content, str(source))
    pin = LibraryPin.from_dict({"name": name, "kind": kind, "file": source.relative_to(root).as_posix(),
                                "sha256": hashlib.sha256(content).hexdigest()})
    registry = load_registry(registry_file) if registry_file.exists() else LibraryRegistry()
    registry.libraries = [old for old in registry.libraries if old.name != pin.name] + [pin]
    atomic_write_json(registry.to_dict(), registry_file)
    return pin


def load_pinned_library(registry_path: str | Path, name: str) -> FootprintInventory | SymbolLibraryInventory:
    registry_file = Path(registry_path)
    registry = load_registry(registry_file)
    matches = [pin for pin in registry.libraries if pin.name == name]
    if not matches:
        raise LibraryError(f"library {name!r} is not registered")
    pin = matches[0]
    root = registry_file.parent.resolve()
    source = (root / pin.file).resolve()
    if not source.is_relative_to(root):
        raise LibraryError("library symlink escapes the registry directory")
    content = _read(source)
    if hashlib.sha256(content).hexdigest() != pin.sha256:
        raise LibraryError("library content differs from its SHA-256 pin")
    return (_footprint if pin.kind == "footprint" else _symbol_library)(content, str(source))
