"""Strict, dependency-free representation of a block-based PCB design.

JSON is the interchange boundary.  Unknown fields and invalid supplied values
are rejected rather than silently coerced.  Electrical correctness belongs to
``pcb_dev.checks``; this module checks structural correctness.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import keyword
import math
from pathlib import Path
import re
from typing import Any, Iterator


class ModelError(ValueError):
    """A design does not satisfy the versioned data contract."""


_ID = re.compile(r"^[A-Za-z0-9_-]+$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
MAX_DESIGN_BYTES = 16 * 1024 * 1024
MAX_BLOCK_DEPTH = 64


@dataclass(slots=True)
class Board:
    width_mm: float
    height_mm: float
    layers: int = 2
    manufacturer: str = "jlcpcb"


@dataclass(slots=True)
class Port:
    id: str
    kind: str
    direction: str
    voltage_min: float | None = None
    voltage_max: float | None = None
    max_current_ma: float | None = None
    current_draw_ma: float = 0
    logic_standard: str | None = None
    protocol: str | None = None
    required: bool = True
    polarity: str | None = None
    impedance_ohm: float | None = None


@dataclass(slots=True)
class Position:
    x_mm: float
    y_mm: float
    rotation: float = 0
    side: str = "top"


@dataclass(slots=True)
class Component:
    id: str
    reference: str
    value: str
    footprint: str
    pins: dict[str, str] = field(default_factory=dict)
    parameters: dict[str, Any] = field(default_factory=dict)
    mpn: str = ""
    lcsc: str = ""
    fitted: bool = True
    position: Position | None = None


@dataclass(slots=True)
class Constraint:
    expression: str
    message: str


@dataclass(slots=True)
class Block:
    id: str
    version: str = "0.1.0"
    description: str = ""
    parameters: dict[str, Any] = field(default_factory=dict)
    ports: list[Port] = field(default_factory=list)
    children: list[Block] = field(default_factory=list)
    components: list[Component] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    equations: dict[str, str] = field(default_factory=dict)
    constraints: list[Constraint] = field(default_factory=list)
    frozen: bool = False
    frozen_hash: str | None = None
    resources: dict[str, Any] = field(default_factory=dict)
    variants: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Connection:
    source: str
    target: str
    net: str


@dataclass(slots=True)
class Design:
    name: str
    version: str
    board: Board
    root: Block
    connections: list[Connection] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    schema_version: int = 1

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Design:
        """Parse v1 JSON data and reject inconsistent global references."""
        obj = _object(data, "$", {
            "schema_version", "name", "version", "board", "root",
            "connections", "metadata",
        }, {"schema_version", "name", "version", "board", "root"})
        if type(obj["schema_version"]) is not int or obj["schema_version"] != 1:
            raise ModelError("$.schema_version: supported version is the integer 1")
        design = cls(
            schema_version=1,
            name=_string(obj["name"], "$.name", nonempty=True),
            version=_string(obj["version"], "$.version", nonempty=True),
            board=_board(obj["board"], "$.board"),
            root=_block(obj["root"], "$.root", 0),
            connections=[_connection(item, f"$.connections[{index}]")
                         for index, item in enumerate(_array(obj.get("connections", []), "$.connections"))],
            metadata=_json_object(obj.get("metadata", {}), "$.metadata"),
        )
        design._validate_references()
        # Imported lazily so graph utilities can use the public model types.
        from .graph import dependency_order
        dependency_order(design)
        return design

    def _validate_references(self) -> None:
        ports = dict(walk_ports(self))
        references: dict[str, str] = {}
        for path, component in walk_components(self):
            key = component.reference.casefold()
            if key in references:
                raise ModelError(
                    f"{path}: duplicate component reference {component.reference!r}; "
                    f"already used at {references[key]}"
                )
            references[key] = path
        seen: set[tuple[str, str, str]] = set()
        for index, connection in enumerate(self.connections):
            for field_name in ("source", "target"):
                endpoint = getattr(connection, field_name)
                if endpoint not in ports:
                    raise ModelError(f"$.connections[{index}].{field_name}: unknown port {endpoint!r}")
            key = (*sorted((connection.source, connection.target)), connection.net)
            if key in seen:
                raise ModelError(f"$.connections[{index}]: duplicate connection on net {connection.net!r}")
            seen.add(key)

    def walk_blocks(self) -> Iterator[tuple[str, Block]]:
        return walk_blocks(self.root)

    def to_dict(self) -> dict[str, Any]:
        """Return JSON-compatible data, explicitly including v1 defaults."""
        return {
            "schema_version": self.schema_version,
            "name": self.name,
            "version": self.version,
            "board": {"width_mm": self.board.width_mm, "height_mm": self.board.height_mm,
                      "layers": self.board.layers, "manufacturer": self.board.manufacturer},
            "root": block_to_dict(self.root),
            "connections": [{"source": c.source, "target": c.target, "net": c.net}
                            for c in self.connections],
            "metadata": _copy_json(self.metadata),
        }

    def fingerprint(self) -> str:
        """SHA-256 of the full canonical design, including freeze records."""
        return _fingerprint(self.to_dict())


def walk_blocks(root: Block, parent_path: str = "") -> Iterator[tuple[str, Block]]:
    """Walk in source order; all paths include the root block's identifier."""
    path = f"{parent_path}.{root.id}" if parent_path else root.id
    yield path, root
    for child in root.children:
        yield from walk_blocks(child, path)


def walk_ports(design: Design) -> Iterator[tuple[str, Port]]:
    for path, block in design.walk_blocks():
        for port in block.ports:
            yield f"{path}.{port.id}", port


def walk_components(design: Design) -> Iterator[tuple[str, Component]]:
    for path, block in design.walk_blocks():
        for component in block.components:
            yield f"{path}.{component.id}", component


def block_to_dict(block: Block, *, include_frozen: bool = True) -> dict[str, Any]:
    data = {
        "id": block.id, "version": block.version, "description": block.description,
        "parameters": _copy_json(block.parameters),
        "ports": [{"id": p.id, "kind": p.kind, "direction": p.direction,
                   "voltage_min": p.voltage_min, "voltage_max": p.voltage_max,
                   "max_current_ma": p.max_current_ma, "current_draw_ma": p.current_draw_ma,
                   "logic_standard": p.logic_standard, "protocol": p.protocol,
                   "required": p.required, "polarity": p.polarity,
                   "impedance_ohm": p.impedance_ohm} for p in block.ports],
        "children": [block_to_dict(c, include_frozen=include_frozen) for c in block.children],
        "components": [{"id": c.id, "reference": c.reference, "value": c.value,
                        "footprint": c.footprint, "pins": dict(c.pins),
                        "parameters": _copy_json(c.parameters), "mpn": c.mpn,
                        "lcsc": c.lcsc, "fitted": c.fitted,
                        "position": None if c.position is None else {
                            "x_mm": c.position.x_mm, "y_mm": c.position.y_mm,
                            "rotation": c.position.rotation, "side": c.position.side}}
                       for c in block.components],
        "depends_on": list(block.depends_on), "equations": dict(block.equations),
        "constraints": [{"expression": c.expression, "message": c.message}
                        for c in block.constraints],
        "resources": _copy_json(block.resources), "variants": _copy_json(block.variants),
    }
    if include_frozen:
        data.update(frozen=block.frozen, frozen_hash=block.frozen_hash)
    return data


def canonical_json(data: Any) -> str:
    """A stable encoding used by hashes and deterministic build manifests."""
    try:
        return json.dumps(data, sort_keys=True, ensure_ascii=False, allow_nan=False,
                          separators=(",", ":"))
    except (TypeError, ValueError, OverflowError) as exc:
        raise ModelError(f"data cannot be represented as finite JSON: {exc}") from exc


def _fingerprint(data: Any) -> str:
    return hashlib.sha256(canonical_json(data).encode("utf-8")).hexdigest()


def block_fingerprint(block: Block) -> str:
    """Hash intrinsic subtree content without self-referential freeze metadata."""
    return _fingerprint(block_to_dict(block, include_frozen=False))


def freeze_fingerprint(
    design: Design, block_path: str,
    resolved_parameters: dict[str, dict[str, Any]] | None = None,
) -> str:
    """Hash a subtree plus inherited, dependency, and shared-net context.

    Changes in a dependency or another branch on a shared global net invalidate
    the block. Shared grounds may deliberately make this closure broad. The hash
    excludes freeze records, so recording it cannot invalidate itself.
    """
    from .expressions import resolve_parameters
    from .graph import dependency_order
    dependency_order(design)
    blocks = dict(design.walk_blocks())
    if block_path not in blocks:
        raise ModelError(f"unknown block path {block_path!r}")
    values = resolve_parameters(design) if resolved_parameters is None else resolved_parameters
    subtree = {path for path in blocks if path == block_path or path.startswith(block_path + ".")}
    block_nets: dict[str, set[str]] = {path: set() for path in blocks}
    net_blocks: dict[str, set[str]] = {}
    for path, block in blocks.items():
        for component in block.components:
            block_nets[path].update(component.pins.values())
    for connection in design.connections:
        block_nets[connection.source.rsplit(".", 1)[0]].add(connection.net)
        block_nets[connection.target.rsplit(".", 1)[0]].add(connection.net)
    for path, nets in block_nets.items():
        for net in nets:
            net_blocks.setdefault(net, set()).add(path)
    included = set(subtree)
    pending = list(subtree)
    while pending:
        path = pending.pop()
        context = set(blocks[path].depends_on)
        context.update(candidate for candidate in blocks if candidate.startswith(path + "."))
        for net in block_nets[path]:
            context.update(net_blocks[net])
        for dependency in context:
            if dependency not in included:
                included.add(dependency)
                pending.append(dependency)
    connections = [
        {"source": c.source, "target": c.target, "net": c.net}
        for c in design.connections
        if any(c.source.rsplit(".", 1)[0] == path or c.target.rsplit(".", 1)[0] == path
               for path in included)
    ]
    payload = {
        "board": {"width_mm": design.board.width_mm, "height_mm": design.board.height_mm,
                  "layers": design.board.layers, "manufacturer": design.board.manufacturer},
        "block": block_to_dict(blocks[block_path], include_frozen=False),
        "context": {path: {"parameters": values[path],
                           "content": block_fingerprint(blocks[path])}
                    for path in sorted(included)},
        "connections": sorted(connections, key=lambda c: (c["net"], c["source"], c["target"])),
    }
    return _fingerprint(payload)


def _reject_constant(value: str) -> None:
    raise ModelError(f"non-finite JSON number {value!r} is not permitted")


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ModelError(f"duplicate JSON object key {key!r}")
        result[key] = value
    return result


def load_design(path: str | Path) -> Design:
    """Read strict UTF-8 JSON, rejecting duplicate keys and excessive files."""
    source = Path(path)
    try:
        with source.open("rb") as stream:
            data = stream.read(MAX_DESIGN_BYTES + 1)
        if len(data) > MAX_DESIGN_BYTES:
            raise ModelError(f"{source}: design exceeds {MAX_DESIGN_BYTES} bytes")
        obj = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_pairs,
                         parse_constant=_reject_constant)
        return Design.from_dict(obj)
    except ModelError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ModelError(f"{source}: cannot load design: {exc}") from exc


def dump_design(design: Design, path: str | Path) -> None:
    """Write deterministic, readable UTF-8 JSON after validating the model."""
    data = design.to_dict()
    Design.from_dict(data)  # Direct dataclass construction is validated at IO.
    destination = Path(path)
    try:
        text = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n"
        destination.write_text(text, encoding="utf-8")
    except (OSError, TypeError, ValueError, OverflowError) as exc:
        raise ModelError(f"{destination}: cannot write design: {exc}") from exc


def _object(value: Any, path: str, allowed: set[str], required: set[str] = frozenset()) -> dict[str, Any]:
    if type(value) is not dict:
        raise ModelError(f"{path}: expected an object")
    if any(type(key) is not str for key in value):
        raise ModelError(f"{path}: object keys must be strings")
    unknown = set(value) - allowed
    if unknown:
        raise ModelError(f"{path}: unknown field(s): {', '.join(sorted(unknown))}")
    missing = required - set(value)
    if missing:
        raise ModelError(f"{path}: missing field(s): {', '.join(sorted(missing))}")
    return value


def _array(value: Any, path: str) -> list[Any]:
    if type(value) is not list:
        raise ModelError(f"{path}: expected an array")
    return value


def _string(value: Any, path: str, *, nonempty: bool = False) -> str:
    if type(value) is not str or (nonempty and not value.strip()):
        raise ModelError(f"{path}: expected a {'non-empty ' if nonempty else ''}string")
    if any(0xD800 <= ord(char) <= 0xDFFF for char in value):
        raise ModelError(f"{path}: unpaired Unicode surrogate is not permitted")
    return value


def _identifier(value: Any, path: str) -> str:
    value = _string(value, path, nonempty=True)
    if not _ID.fullmatch(value):
        raise ModelError(f"{path}: identifiers use letters, digits, '_' and '-', without dots")
    return value


def _bool(value: Any, path: str) -> bool:
    if type(value) is not bool:
        raise ModelError(f"{path}: expected a boolean")
    return value


def _number(value: Any, path: str, *, minimum: float | None = None, positive: bool = False) -> float:
    if type(value) not in (int, float):
        raise ModelError(f"{path}: expected a number (booleans are not numbers)")
    try:
        finite = math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite:
        raise ModelError(f"{path}: number must be finite")
    if positive and value <= 0:
        raise ModelError(f"{path}: number must be greater than zero")
    if minimum is not None and value < minimum:
        raise ModelError(f"{path}: number must be at least {minimum}")
    return value


def _optional_number(value: Any, path: str, **kwargs: Any) -> float | None:
    return None if value is None else _number(value, path, **kwargs)


def _optional_string(value: Any, path: str) -> str | None:
    return None if value is None else _string(value, path, nonempty=True)


def _choice(value: Any, path: str, choices: set[str]) -> str:
    value = _string(value, path)
    if value not in choices:
        raise ModelError(f"{path}: expected one of {', '.join(sorted(choices))}")
    return value


def _json_value(value: Any, path: str, depth: int = 0) -> Any:
    if depth > MAX_BLOCK_DEPTH:
        raise ModelError(f"{path}: JSON nesting exceeds {MAX_BLOCK_DEPTH}")
    if value is None or type(value) is bool:
        return value
    if type(value) is str:
        return _string(value, path)
    if type(value) in (int, float):
        return _number(value, path)
    if type(value) is list:
        return [_json_value(item, f"{path}[{i}]", depth + 1) for i, item in enumerate(value)]
    if type(value) is dict:
        result = {}
        for key, item in value.items():
            _string(key, path, nonempty=True)
            result[key] = _json_value(item, f"{path}.{key}", depth + 1)
        return result
    raise ModelError(f"{path}: expected a JSON value")


def _json_object(value: Any, path: str) -> dict[str, Any]:
    if type(value) is not dict:
        raise ModelError(f"{path}: expected an object")
    return _json_value(value, path)


def _copy_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _copy_json(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_copy_json(v) for v in value]
    return value


def _board(value: Any, path: str) -> Board:
    obj = _object(value, path, {"width_mm", "height_mm", "layers", "manufacturer"}, {"width_mm", "height_mm"})
    layers = obj.get("layers", 2)
    if type(layers) is not int or layers not in (2, 4):
        raise ModelError(f"{path}.layers: expected integer 2 or 4")
    return Board(
        width_mm=_number(obj["width_mm"], f"{path}.width_mm", positive=True),
        height_mm=_number(obj["height_mm"], f"{path}.height_mm", positive=True),
        layers=layers,
        manufacturer=_choice(obj.get("manufacturer", "jlcpcb"), f"{path}.manufacturer", {"jlcpcb"}),
    )


def _port(value: Any, path: str) -> Port:
    obj = _object(value, path, {
        "id", "kind", "direction", "voltage_min", "voltage_max", "max_current_ma",
        "current_draw_ma", "logic_standard", "protocol", "required", "polarity", "impedance_ohm",
    }, {"id", "kind", "direction"})
    port = Port(
        id=_identifier(obj["id"], f"{path}.id"),
        kind=_choice(obj["kind"], f"{path}.kind", {"power", "ground", "digital", "analog", "differential", "bus"}),
        direction=_choice(obj["direction"], f"{path}.direction", {"input", "output", "bidirectional", "passive"}),
        voltage_min=_optional_number(obj.get("voltage_min"), f"{path}.voltage_min"),
        voltage_max=_optional_number(obj.get("voltage_max"), f"{path}.voltage_max"),
        max_current_ma=_optional_number(obj.get("max_current_ma"), f"{path}.max_current_ma", minimum=0),
        current_draw_ma=_number(obj.get("current_draw_ma", 0), f"{path}.current_draw_ma", minimum=0),
        logic_standard=_optional_string(obj.get("logic_standard"), f"{path}.logic_standard"),
        protocol=_optional_string(obj.get("protocol"), f"{path}.protocol"),
        required=_bool(obj.get("required", True), f"{path}.required"),
        polarity=_optional_string(obj.get("polarity"), f"{path}.polarity"),
        impedance_ohm=_optional_number(obj.get("impedance_ohm"), f"{path}.impedance_ohm", positive=True),
    )
    if port.voltage_min is not None and port.voltage_max is not None and port.voltage_min > port.voltage_max:
        raise ModelError(f"{path}: voltage_min exceeds voltage_max")
    return port


def _position(value: Any, path: str) -> Position | None:
    if value is None:
        return None
    obj = _object(value, path, {"x_mm", "y_mm", "rotation", "side"}, {"x_mm", "y_mm"})
    return Position(
        x_mm=_number(obj["x_mm"], f"{path}.x_mm"),
        y_mm=_number(obj["y_mm"], f"{path}.y_mm"),
        rotation=_number(obj.get("rotation", 0), f"{path}.rotation"),
        side=_choice(obj.get("side", "top"), f"{path}.side", {"top", "bottom"}),
    )


def _component(value: Any, path: str) -> Component:
    obj = _object(value, path, {
        "id", "reference", "value", "footprint", "pins", "parameters", "mpn", "lcsc", "fitted", "position",
    }, {"id", "reference", "value", "footprint", "pins"})
    if type(obj["pins"]) is not dict:
        raise ModelError(f"{path}.pins: expected an object")
    pins = {_string(key, f"{path}.pins", nonempty=True):
            _string(net, f"{path}.pins.{key}", nonempty=True) for key, net in obj["pins"].items()}
    return Component(
        id=_identifier(obj["id"], f"{path}.id"),
        reference=_string(obj["reference"], f"{path}.reference", nonempty=True),
        value=_string(obj["value"], f"{path}.value"),
        footprint=_string(obj["footprint"], f"{path}.footprint"), pins=pins,
        parameters=_json_object(obj.get("parameters", {}), f"{path}.parameters"),
        mpn=_string(obj.get("mpn", ""), f"{path}.mpn"),
        lcsc=_string(obj.get("lcsc", ""), f"{path}.lcsc"),
        fitted=_bool(obj.get("fitted", True), f"{path}.fitted"),
        position=_position(obj.get("position"), f"{path}.position"),
    )


def _constraint(value: Any, path: str) -> Constraint:
    obj = _object(value, path, {"expression", "message"}, {"expression", "message"})
    return Constraint(_string(obj["expression"], f"{path}.expression", nonempty=True),
                      _string(obj["message"], f"{path}.message", nonempty=True))


def _block(value: Any, path: str, depth: int) -> Block:
    if depth > MAX_BLOCK_DEPTH:
        raise ModelError(f"{path}: block nesting exceeds {MAX_BLOCK_DEPTH}")
    obj = _object(value, path, {
        "id", "version", "description", "parameters", "ports", "children", "components",
        "depends_on", "equations", "constraints", "frozen", "frozen_hash", "resources", "variants",
    }, {"id"})
    equations = _json_object(obj.get("equations", {}), f"{path}.equations")
    for target, expression in equations.items():
        if not target.isidentifier() or keyword.iskeyword(target) or target.startswith("__"):
            raise ModelError(f"{path}.equations: invalid equation target {target!r}")
        _string(expression, f"{path}.equations.{target}", nonempty=True)
    depends = [_string(item, f"{path}.depends_on[{i}]", nonempty=True)
               for i, item in enumerate(_array(obj.get("depends_on", []), f"{path}.depends_on"))]
    if len(depends) != len(set(depends)):
        raise ModelError(f"{path}.depends_on: duplicate dependency")
    frozen_hash = obj.get("frozen_hash")
    if frozen_hash is not None:
        frozen_hash = _string(frozen_hash, f"{path}.frozen_hash")
        if not _SHA256.fullmatch(frozen_hash):
            raise ModelError(f"{path}.frozen_hash: expected a lowercase SHA-256 digest")
    block = Block(
        id=_identifier(obj["id"], f"{path}.id"),
        version=_string(obj.get("version", "0.1.0"), f"{path}.version", nonempty=True),
        description=_string(obj.get("description", ""), f"{path}.description"),
        parameters=_json_object(obj.get("parameters", {}), f"{path}.parameters"),
        ports=[_port(item, f"{path}.ports[{i}]")
               for i, item in enumerate(_array(obj.get("ports", []), f"{path}.ports"))],
        children=[_block(item, f"{path}.children[{i}]", depth + 1)
                  for i, item in enumerate(_array(obj.get("children", []), f"{path}.children"))],
        components=[_component(item, f"{path}.components[{i}]")
                    for i, item in enumerate(_array(obj.get("components", []), f"{path}.components"))],
        depends_on=depends, equations=equations,
        constraints=[_constraint(item, f"{path}.constraints[{i}]")
                     for i, item in enumerate(_array(obj.get("constraints", []), f"{path}.constraints"))],
        frozen=_bool(obj.get("frozen", False), f"{path}.frozen"), frozen_hash=frozen_hash,
        resources=_json_object(obj.get("resources", {}), f"{path}.resources"),
        variants=_json_object(obj.get("variants", {}), f"{path}.variants"),
    )
    seen: set[str] = set()
    for item in [*block.ports, *block.children, *block.components]:
        if item.id in seen:
            raise ModelError(f"{path}: duplicate member identifier {item.id!r}")
        seen.add(item.id)
    return block


def _connection(value: Any, path: str) -> Connection:
    obj = _object(value, path, {"source", "target", "net"}, {"source", "target", "net"})
    return Connection(*(_string(obj[key], f"{path}.{key}", nonempty=True)
                        for key in ("source", "target", "net")))
