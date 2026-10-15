"""Bind every model component to captured, pinned KiCad source data.

All validation is completed without writing output. There is no synthetic symbol
or footprint fallback. Captured source and attribution bytes let a publisher
bundle exactly the content that was hashed and parsed, without reopening files.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import re
from typing import Any

from .libraries import (
    FootprintInventory, LibraryRegistry, SymbolInventory,
    _footprint, _symbol_library, check_footprint, check_symbol_footprint,
)
from .model import Component, Design, canonical_json, walk_components
from .sexpr import Quoted, dumps, parse_quoted


class BindingsError(ValueError):
    """Missing, unsafe, unpinned, or unsupported library-backed component data."""


MAX_BINDINGS_BYTES = 16 * 1024 * 1024
MAX_SOURCE_BYTES = 32 * 1024 * 1024
MAX_TOTAL_SOURCE_BYTES = 128 * 1024 * 1024
MAX_LICENSE_BYTES = 1024 * 1024
MAX_LICENSE_FILES = 64
_LIB_ID = re.compile(r"^[A-Za-z0-9_.+~-]+:[A-Za-z0-9_.+~-]+$")


@dataclass(slots=True)
class BoundComponent:
    path: str
    component: Component
    symbol_id: str
    footprint_id: str
    symbol_name: str
    symbol_tree: list
    footprint_tree: list
    symbol_inventory: SymbolInventory
    footprint_inventory: FootprintInventory
    no_connect_pins: tuple[str, ...]
    unit: int = 1


@dataclass(slots=True)
class BoundDesign:
    design: Design
    components: dict[str, BoundComponent]
    provenance: dict[str, Any]
    bindings_content: bytes = b""
    registry_content: bytes = b""
    source_contents: dict[str, bytes] = field(default_factory=dict)
    license_contents: dict[str, bytes] = field(default_factory=dict)


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _capture(path: Path, limit: int, label: str) -> bytes:
    try:
        if not path.is_file():
            raise BindingsError(f"{label}: expected a regular file")
        with path.open("rb") as stream:
            content = stream.read(limit + 1)
        if not content or len(content) > limit:
            raise BindingsError(f"{label}: file must contain 1..{limit} bytes")
        return content
    except OSError as exc:
        raise BindingsError(f"{label}: cannot read file: {exc}") from exc


def _json(content: bytes, label: str) -> Any:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result = {}
        for key, value in items:
            if key in result:
                raise BindingsError(f"{label}: duplicate JSON key {key!r}")
            result[key] = value
        return result

    def constant(value: str) -> None:
        raise BindingsError(f"{label}: non-finite JSON constant {value!r}")

    try:
        result = json.loads(content.decode("utf-8-sig"), object_pairs_hook=pairs, parse_constant=constant)
        canonical_json(result)
        return result
    except (ValueError, UnicodeError, RecursionError) as exc:
        if isinstance(exc, BindingsError):
            raise
        raise BindingsError(f"{label}: invalid finite UTF-8 JSON: {exc}") from exc


def _fields(value: Any, required: set[str], optional: set[str], label: str) -> dict[str, Any]:
    if type(value) is not dict:
        raise BindingsError(f"{label}: expected an object")
    if set(value) - required - optional or required - set(value):
        raise BindingsError(f"{label}: requires fields {', '.join(sorted(required))}; "
                            f"optional: {', '.join(sorted(optional)) or '<none>'}")
    return value


def _string(value: Any, label: str) -> str:
    if type(value) is not str or not value.strip() or any(ord(char) < 32 for char in value):
        raise BindingsError(f"{label}: expected a non-empty string without control characters")
    try:
        value.encode("utf-8")
    except UnicodeError as exc:
        raise BindingsError(f"{label}: invalid Unicode") from exc
    return value


def _relative(value: Any, label: str) -> str:
    value = _string(value, label)
    if ("\\" in value or ":" in value or PurePosixPath(value).is_absolute()
            or any(part in ("", ".", "..") for part in value.split("/"))):
        raise BindingsError(f"{label}: expected a contained relative path without traversal")
    return value


def _contained(root: Path, relative: str, label: str) -> Path:
    _relative(relative, label)
    try:
        path = (root / relative).resolve(strict=True)
    except (OSError, ValueError, RuntimeError) as exc:
        raise BindingsError(f"{label}: missing or invalid local path") from exc
    if not path.is_relative_to(root):
        raise BindingsError(f"{label}: symlink escapes its containing directory")
    return path


def _lib_id(value: Any, label: str) -> str:
    value = _string(value, label)
    if not _LIB_ID.fullmatch(value):
        raise BindingsError(f"{label}: expected a KiCad Library:Name identifier using ASCII letters, digits, '_.+~-'")
    return value


def _children(node: list, tag: str) -> list[list]:
    return [item for item in node[2:] if type(item) is list and item and item[0] == tag]


def _single(node: list, tag: str, *, required: bool = False) -> list | None:
    result = [item for item in node[1:] if type(item) is list and item and item[0] == tag]
    if len(result) > 1 or (required and not result):
        raise BindingsError(f"source requires {'one' if required else 'at most one'} {tag!r} field")
    return result[0] if result else None


def _quoted_name(node: list, label: str) -> str:
    if len(node) < 2 or not isinstance(node[1], Quoted) or not node[1]:
        raise BindingsError(f"{label}: expected a quoted non-empty name")
    return str(node[1])


def _unit(node: list) -> tuple[int, int]:
    name = _quoted_name(node, "symbol unit")
    parts = name.rsplit("_", 2)
    if (len(parts) != 3 or not parts[0]
            or not all(part.isascii() and part.isdecimal() for part in parts[-2:])):
        raise BindingsError(f"unsupported symbol unit name {name!r}")
    unit, convert = int(parts[-2]), int(parts[-1])
    if unit not in (0, 1) or convert not in (0, 1):
        raise BindingsError(f"unsupported multi-unit or alternate-conversion symbol {name!r}; only units 0/1 and conversions 0/1 are supported")
    return unit, convert


def _validate_tree_tags(tree: list) -> None:
    pending = [tree]
    while pending:
        node = pending.pop()
        if not node or type(node[0]) is not str:
            raise BindingsError("KiCad source expression tags must be bare non-empty atoms")
        pending.extend(item for item in node[1:] if type(item) is list)


def _source_symbols(tree: list) -> dict[str, list]:
    if not tree or tree[0] != "kicad_symbol_lib":
        raise BindingsError("symbol source is not a KiCad symbol library")
    result = {}
    for item in tree[1:]:
        if type(item) is list and item and item[0] == "symbol":
            name = _quoted_name(item, "source symbol")
            if name in result:
                raise BindingsError(f"duplicate source symbol {name!r}")
            result[name] = item
    return result


def _root_fields(symbol: list) -> dict[tuple[str, str], list]:
    fields = {}
    for node in symbol[2:]:
        if type(node) is not list or not node or type(node[0]) is not str:
            raise BindingsError("symbol fields must be named expressions")
        tag = node[0]
        if tag == "extends":
            continue
        if tag == "property":
            if len(node) < 3 or not isinstance(node[1], Quoted) or not isinstance(node[2], Quoted):
                raise BindingsError("symbol properties require quoted names and values")
            key = tag, str(node[1])
        elif tag == "symbol":
            unit, convert = _unit(node)
            key = tag, f"{unit}_{convert}"
        elif tag == "pin":
            number = _single(node, "number", required=True)
            if len(number) < 2:
                raise BindingsError("symbol pin number is missing")
            key = tag, str(number[1])
        else:
            key = tag, ""
        if key in fields:
            raise BindingsError(f"duplicate source symbol field {key!r}")
        fields[key] = node
    return fields


def _resolve_symbol(table: dict[str, list], name: str) -> list:
    chain = []
    visited = set()
    current = name
    while True:
        if current not in table:
            raise BindingsError(f"source symbol parent {current!r} is unavailable in the pinned library")
        if current in visited:
            raise BindingsError("symbol inheritance cycle: " + " -> ".join(chain + [current]))
        if len(chain) >= 64:
            raise BindingsError("symbol inheritance exceeds 64 levels")
        visited.add(current)
        chain.append(current)
        symbol = table[current]
        extends = _single(symbol, "extends")
        if extends is None:
            break
        if len(extends) != 2 or not isinstance(extends[1], Quoted):
            raise BindingsError("symbol extends requires one quoted local parent")
        if _children(symbol, "symbol") or _children(symbol, "pin"):
            raise BindingsError("derived symbols with their own graphics/pins are unsupported; no inheritance override is guessed")
        current = str(extends[1])
    merged: dict[tuple[str, str], list] = {}
    for current in reversed(chain):
        merged.update(_root_fields(table[current]))
    return ["symbol", Quoted(name), *copy.deepcopy(list(merged.values()))]


def _embed_symbol(table: dict[str, list], name: str, symbol_id: str) -> list:
    symbol = _resolve_symbol(table, name)
    symbol[1] = Quoted(symbol_id)
    basename = symbol_id.split(":", 1)[1]
    seen = set()
    for unit in _children(symbol, "symbol"):
        identity = _unit(unit)
        if identity in seen:
            raise BindingsError(f"duplicate embedded symbol unit {identity}")
        seen.add(identity)
        if _children(unit, "symbol"):
            raise BindingsError("nested symbol units are unsupported")
        unit[1] = Quoted(f"{basename}_{identity[0]}_{identity[1]}")
    return symbol


def _pin_signature(pin: Any) -> tuple:
    return (pin.name, pin.electrical_type, pin.shape, pin.x_mm, pin.y_mm,
            pin.rotation % 360, pin.length_mm, pin.hidden)


def _validate_pins(path: str, component: Component, symbol: SymbolInventory,
                   footprint: FootprintInventory, no_connect_pins: tuple[str, ...]) -> None:
    pins = {}
    for pin in symbol.pins:
        if pin.unit not in (0, 1) or pin.convert not in (0, 1):
            raise BindingsError(f"{path}: unsupported symbol unit/conversion")
        signature = _pin_signature(pin)
        if pin.number in pins and pins[pin.number] != signature:
            raise BindingsError(f"{path}: symbol pin {pin.number!r} has conflicting duplicate definitions")
        pins[pin.number] = signature
    if not pins:
        raise BindingsError(f"{path}: source symbol has no electrical pins")
    connected, disconnected = set(component.pins), set(no_connect_pins)
    if connected & disconnected:
        raise BindingsError(f"{path}: a symbol pin cannot be connected and explicitly no-connect")
    unknown = (connected | disconnected) - set(pins)
    missing = set(pins) - connected - disconnected
    if unknown or missing:
        raise BindingsError(f"{path}: incomplete symbol pin map; unknown={sorted(unknown)}, missing={sorted(missing)}")
    report = check_symbol_footprint(symbol, footprint)
    if report.blocking:
        raise BindingsError(f"{path}: symbol/footprint numbered electrical pins differ: "
                            + ", ".join(issue.block_path for issue in report.issues))
    geometry = check_footprint(footprint)
    if geometry.blocking:
        raise BindingsError(f"{path}: footprint has invalid pad/drill geometry: "
                            + ", ".join(issue.code for issue in geometry.issues if issue.severity.value in {"error", "blocker"}))
    for pad in footprint.pads:
        if pad.type not in {"smd", "thru_hole", "np_thru_hole", "connect"}:
            raise BindingsError(f"{path}: unsupported footprint pad type {pad.type!r}")
        if pad.shape not in {"circle", "rect", "oval", "trapezoid", "roundrect", "custom"}:
            raise BindingsError(f"{path}: unsupported footprint pad shape {pad.shape!r}")


def _validate_footprint_tree(tree: list, footprint_id: str) -> None:
    if not tree or tree[0] != "footprint":
        raise BindingsError("footprint source has the wrong root")
    name = _quoted_name(tree, "source footprint")
    if name != footprint_id.split(":", 1)[1]:
        raise BindingsError(f"footprint identifier {footprint_id!r} does not match pinned source name {name!r}")
    layer = _single(tree, "layer", required=True)
    if len(layer) != 2 or not isinstance(layer[1], Quoted) or layer[1] != "F.Cu":
        raise BindingsError("only top-side F.Cu source footprints are supported")
    placement = _single(tree, "at")
    if placement is not None:
        if len(placement) not in (3, 4):
            raise BindingsError("unsupported source footprint placement")
        try:
            coordinates = [float(value) for value in placement[1:]]
        except (ValueError, TypeError, OverflowError) as exc:
            raise BindingsError("unsupported source footprint placement") from exc
        if (any(type(value) is not str for value in placement[1:])
                or any(not math.isfinite(value) or value != 0 for value in coordinates)):
            raise BindingsError("source footprint must have no pre-existing translation/rotation")


def load_bindings(design: Design, path: str | Path) -> BoundDesign:
    """Validate a complete model-to-library binding and capture all used bytes."""
    try:
        design = Design.from_dict(design.to_dict())
        source_path = Path(path).resolve(strict=True)
        base = source_path.parent
        binding_content = _capture(source_path, MAX_BINDINGS_BYTES, "bindings")
        data = _fields(_json(binding_content, "bindings"), {"schema_version", "registry", "components"}, {"licenses"}, "bindings")
        if type(data["schema_version"]) is not int or data["schema_version"] != 1:
            raise BindingsError("supported bindings schema_version is integer 1")
        registry_relative = _relative(data["registry"], "registry")
        registry_path = _contained(base, registry_relative, "registry")
        if registry_path == source_path:
            raise BindingsError("bindings and registry must be separate files")
        registry_content = _capture(registry_path, MAX_BINDINGS_BYTES, "registry")
        registry = LibraryRegistry.from_dict(_json(registry_content, "registry"))
        pins = {pin.name: pin for pin in registry.libraries}
        entries = data["components"]
        if type(entries) is not dict:
            raise BindingsError("bindings.components must be an object keyed by absolute component path")
        components = dict(walk_components(design))
        if set(entries) != set(components):
            raise BindingsError(f"component bindings must exactly match the model; "
                                f"missing={sorted(set(components) - set(entries))}, extra={sorted(set(entries) - set(components))}")
        captured: dict[str, bytes] = {}
        source_paths: set[Path] = set()
        inventories = {}
        trees: dict[str, list] = {}
        provenance = {"bindings_sha256": _sha256(binding_content), "registry": registry_relative,
                      "registry_sha256": _sha256(registry_content), "sources": {}, "licenses": {}}
        total_source_bytes = 0

        def source(alias: Any, kind: str) -> tuple[Any, list]:
            nonlocal total_source_bytes
            alias = _string(alias, "library alias")
            if alias not in pins or pins[alias].kind != kind:
                raise BindingsError(f"library {alias!r} is not pinned as {kind!r}")
            if alias not in captured:
                pin = pins[alias]
                source_file = _contained(registry_path.parent, pin.file, f"source {alias}")
                if source_file in {source_path, registry_path}:
                    raise BindingsError("library sources cannot overlap the bindings or registry file")
                content = _capture(source_file, MAX_SOURCE_BYTES, f"source {alias}")
                if _sha256(content) != pin.sha256:
                    raise BindingsError(f"source {alias!r}: content differs from its SHA-256 pin")
                total_source_bytes += len(content)
                if total_source_bytes > MAX_TOTAL_SOURCE_BYTES:
                    raise BindingsError("used library sources exceed the aggregate size limit")
                inventory = (_footprint if kind == "footprint" else _symbol_library)(content, pin.file)
                tree = parse_quoted(content.decode("utf-8-sig"))
                _validate_tree_tags(tree)
                inventories[alias], trees[alias], captured[alias] = inventory, tree, content
                source_paths.add(source_file)
                provenance["sources"][alias] = {"file": pin.file, "sha256": pin.sha256, "kind": pin.kind}
            return inventories[alias], trees[alias]

        bound_components = {}
        symbol_ids = {}
        entry_fields = {"symbol_library", "symbol_name", "symbol_id", "footprint_library", "unit", "no_connect_pins"}
        for component_path, component in components.items():
            entry = _fields(entries[component_path], entry_fields, set(), component_path)
            if type(entry["unit"]) is not int or entry["unit"] != 1:
                raise BindingsError(f"{component_path}: only symbol unit 1 is supported")
            if component.position is not None and component.position.side != "top":
                raise BindingsError(f"{component_path}: only top-side component placement is supported")
            nc = entry["no_connect_pins"]
            if type(nc) is not list or any(type(pin) is not str or not pin for pin in nc) or len(nc) != len(set(nc)):
                raise BindingsError(f"{component_path}.no_connect_pins requires distinct non-empty pin-number strings")
            no_connect_pins = tuple(sorted(nc))
            symbol_name = _string(entry["symbol_name"], "symbol_name")
            symbol_id = _lib_id(entry["symbol_id"], "symbol_id")
            footprint_id = _lib_id(component.footprint, "component.footprint")
            symbol_library, symbol_source = source(entry["symbol_library"], "symbols")
            footprint_inventory, footprint_source = source(entry["footprint_library"], "footprint")
            matches = [item for item in symbol_library.symbols if item.name == symbol_name]
            if len(matches) != 1:
                raise BindingsError(f"{component_path}: source symbol {symbol_name!r} does not exist")
            symbol_inventory = matches[0]
            symbol_tree = _embed_symbol(_source_symbols(symbol_source), symbol_name, symbol_id)
            _validate_footprint_tree(footprint_source, footprint_id)
            _validate_pins(component_path, component, symbol_inventory, footprint_inventory, no_connect_pins)
            identity = dumps(symbol_tree)
            if symbol_id in symbol_ids and symbol_ids[symbol_id] != identity:
                raise BindingsError(f"symbol_id {symbol_id!r} resolves to conflicting source definitions")
            symbol_ids[symbol_id] = identity
            bound_components[component_path] = BoundComponent(
                component_path, component, symbol_id, footprint_id, symbol_name,
                symbol_tree, copy.deepcopy(footprint_source), copy.deepcopy(symbol_inventory),
                copy.deepcopy(footprint_inventory), no_connect_pins, 1,
            )
        licenses = data.get("licenses", [])
        if type(licenses) is not list or len(licenses) > MAX_LICENSE_FILES:
            raise BindingsError(f"licenses must be an array of at most {MAX_LICENSE_FILES} contained text files")
        license_contents = {}
        license_paths = set()
        for relative in licenses:
            relative = _relative(relative, "license")
            license_path = _contained(base, relative, "license")
            if relative in license_contents or license_path in license_paths:
                raise BindingsError("duplicate license/attribution file")
            if license_path in source_paths | {source_path, registry_path}:
                raise BindingsError("license/attribution files cannot overlap library sources, bindings or registry")
            content = _capture(license_path, MAX_LICENSE_BYTES, "license")
            try:
                text = content.decode("utf-8-sig")
            except UnicodeError as exc:
                raise BindingsError("license/attribution files must be UTF-8 text") from exc
            if "\x00" in text:
                raise BindingsError("license/attribution files must be plain text without NUL bytes")
            license_contents[relative] = content
            license_paths.add(license_path)
            provenance["licenses"][relative] = {"sha256": _sha256(content)}
        return BoundDesign(design, bound_components, provenance, binding_content,
                           registry_content, captured, license_contents)
    except BindingsError:
        raise
    except (ValueError, OSError, TypeError, KeyError, UnicodeError, RecursionError) as exc:
        raise BindingsError(f"invalid library bindings: {exc}") from exc
