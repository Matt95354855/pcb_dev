"""Data-only reusable blocks, explicit assembly variants and pinned packages.

Packages are ordinary bounded JSON files.  Loading never imports Python,
extracts archives, follows URLs, or runs hooks.  A lock binds one exact semantic
version to the SHA-256 of the complete package file.  Lock-relative paths are
contained after symlink resolution.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
import hashlib
from pathlib import Path, PurePosixPath
import re
from typing import Any

from .model import Block, Connection, Design, ModelError, block_to_dict, walk_blocks
from .transactions import atomic_write_json, read_json


class PackageError(ModelError):
    """An unsafe, unpinned, malformed, or incompatible block package."""


_NAME = re.compile(r"^[A-Za-z0-9_-]+$")
_SEMVER = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-((?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)(?:\.(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*))*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
_HASH = re.compile(r"^[0-9a-f]{64}$")
MAX_PACKAGE_BYTES = 16 * 1024 * 1024


def _name(value: Any, field_name: str) -> str:
    if type(value) is not str or not _NAME.fullmatch(value):
        raise PackageError(f"{field_name} must use letters, digits, '_' and '-' only")
    return value


def _version(value: Any) -> str:
    if type(value) is not str or not _SEMVER.fullmatch(value):
        raise PackageError("package version must be an exact semantic version, such as '1.2.3'")
    return value


def _digest(value: Any) -> str:
    if type(value) is not str or not _HASH.fullmatch(value):
        raise PackageError("expected a lowercase SHA-256 digest")
    return value


def _package_design(root: dict[str, Any], connections: list[dict[str, Any]]) -> Design:
    return Design.from_dict({"schema_version": 1, "name": "package-validation", "version": "0.1.0",
                             "board": {"width_mm": 1, "height_mm": 1},
                             "root": root, "connections": connections})


def _variant_paths(root: Block) -> dict[str, Any]:
    prefix = root.id + "."
    found = {}
    for block_path, block in walk_blocks(root):
        for component in block.components:
            found[f"{block_path}.{component.id}".removeprefix(prefix)] = component
    return found


def _validate_variants(root: Block) -> None:
    for _, block in walk_blocks(root):
        components = _variant_paths(block)
        for name, definition in block.variants.items():
            _name(name, "variant name")
            if type(definition) is not dict or set(definition) - {"dnp", "fit", "parameters"}:
                raise PackageError(f"variant {name!r} accepts only dnp, fit and parameters")
            sets = {}
            for key in ("dnp", "fit"):
                paths = definition.get(key, [])
                if type(paths) is not list or any(type(path) is not str for path in paths):
                    raise PackageError(f"variant {name!r}.{key} must be an array of component paths")
                if len(paths) != len(set(paths)) or set(paths) - set(components):
                    raise PackageError(f"variant {name!r}.{key} has a duplicate or unknown relative component path")
                sets[key] = set(paths)
            if sets["dnp"] & sets["fit"]:
                raise PackageError(f"variant {name!r} marks a component as both fit and dnp")
            if type(definition.get("parameters", {})) is not dict:
                raise PackageError(f"variant {name!r}.parameters must be an object")


@dataclass(slots=True)
class BlockPackage:
    name: str
    version: str
    root: Block
    connections: list[Connection] = field(default_factory=list)
    schema_version: int = 1

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BlockPackage:
        fields = {"schema_version", "name", "version", "root", "connections"}
        if type(data) is not dict or set(data) != fields:
            raise PackageError("a package requires exactly schema_version, name, version, root and connections")
        if type(data["schema_version"]) is not int or data["schema_version"] != 1:
            raise PackageError("supported package schema_version is integer 1")
        name, version = _name(data["name"], "package name"), _version(data["version"])
        try:
            design = _package_design(data["root"], data["connections"])
        except ModelError as exc:
            raise PackageError(f"invalid packaged block: {exc}") from exc
        for path, block in design.walk_blocks():
            if block.frozen or block.frozen_hash is not None:
                raise PackageError(f"{path}: packages cannot carry trusted freeze records")
        _validate_variants(design.root)
        return cls(name, version, design.root, design.connections)

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": self.schema_version, "name": self.name, "version": self.version,
                "root": block_to_dict(self.root),
                "connections": [{"source": c.source, "target": c.target, "net": c.net} for c in self.connections]}


def _replace_path(path: str, old_root: str, new_root: str) -> str:
    if type(path) is not str:
        raise PackageError("dependency and resource paths must be strings")
    if path == old_root:
        return new_root
    if path.startswith(old_root + "."):
        return new_root + path[len(old_root):]
    raise PackageError(f"external dependency or resource path {path!r} cannot be packaged implicitly")


def _remap_resource_paths(resources: dict[str, Any], old_root: str, new_root: str) -> None:
    sequence = resources.get("power_sequence")
    if type(sequence) is list:
        resources["power_sequence"] = [_replace_path(path, old_root, new_root) for path in sequence]
    elif type(sequence) is dict and "after" in sequence:
        after = sequence["after"]
        if type(after) is not list or any(type(path) is not str for path in after):
            raise PackageError("power_sequence.after must be a list of internal block paths")
        sequence["after"] = [_replace_path(path, old_root, new_root) for path in after]
    reservations = resources.get("i2c", [])
    entries = [reservations] if type(reservations) is dict else reservations
    if type(entries) is list:
        for entry in entries:
            if (type(entry) is dict and type(entry.get("bus")) is str
                    and (entry["bus"] == old_root or entry["bus"].startswith(old_root + "."))):
                entry["bus"] = _replace_path(entry["bus"], old_root, new_root)


def create_package(design: Design, block_path: str, *, name: str, version: str | None = None) -> BlockPackage:
    """Extract one subtree and its internal connections; external links stay interfaces."""
    design = Design.from_dict(design.to_dict())
    blocks = dict(design.walk_blocks())
    if block_path not in blocks:
        raise PackageError(f"unknown block path {block_path!r}")
    root = block_to_dict(blocks[block_path], include_frozen=False)
    # A reusable subtree carries the effective boundary values it previously
    # inherited. Its own equations still recalculate when an instance overrides.
    from .expressions import resolve_parameters
    root["parameters"] = copy.deepcopy(resolve_parameters(design)[block_path])
    root_id = root["id"]
    pending = [root]
    while pending:
        block = pending.pop()
        block["depends_on"] = [_replace_path(path, block_path, root_id) for path in block["depends_on"]]
        _remap_resource_paths(block["resources"], block_path, root_id)
        pending.extend(block["children"])
    prefix = block_path + "."
    connections = [{"source": _replace_path(c.source, block_path, root_id),
                    "target": _replace_path(c.target, block_path, root_id), "net": c.net}
                   for c in design.connections if c.source.startswith(prefix) and c.target.startswith(prefix)]
    return BlockPackage.from_dict({"schema_version": 1, "name": name,
                                   "version": blocks[block_path].version if version is None else version,
                                   "root": root, "connections": connections})


def write_package(package: BlockPackage, path: str | Path) -> str:
    """Write deterministic package data and return the complete file's SHA-256."""
    validated = BlockPackage.from_dict(package.to_dict())
    atomic_write_json(validated.to_dict(), path)
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_package(path: str | Path, *, expected_version: str, expected_sha256: str) -> BlockPackage:
    """Verify exact version and file digest before trusting any package content."""
    _version(expected_version)
    _digest(expected_sha256)
    source = Path(path)
    try:
        with source.open("rb") as stream:
            content = stream.read(MAX_PACKAGE_BYTES + 1)
        if len(content) > MAX_PACKAGE_BYTES:
            raise PackageError(f"package exceeds {MAX_PACKAGE_BYTES} bytes")
        if hashlib.sha256(content).hexdigest() != expected_sha256:
            raise PackageError(f"{source}: package SHA-256 does not match the pinned lock")
        # Parse exactly the bytes that were hashed, avoiding a reopen race.
        import json
        def pairs(items):
            result = {}
            for key, value in items:
                if key in result:
                    raise PackageError(f"duplicate package JSON key {key!r}")
                result[key] = value
            return result
        def constant(value):
            raise PackageError(f"non-finite package JSON constant {value!r}")
        data = json.loads(content.decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant)
        package = BlockPackage.from_dict(data)
        if package.version != expected_version:
            raise PackageError(f"package version {package.version!r} differs from pinned {expected_version!r}")
        return package
    except PackageError:
        raise
    except (OSError, ValueError, UnicodeError, RecursionError) as exc:
        raise PackageError(f"cannot load package {source}: {exc}") from exc


def apply_variant(block: Block, variant: str) -> Block:
    """Copy a block and explicitly mark relative component paths fit or DNP."""
    if any(item.frozen for _, item in walk_blocks(block)):
        raise PackageError("cannot apply an assembly variant to a frozen block")
    design = _package_design(block_to_dict(block), [])
    copied = design.root
    _validate_variants(copied)
    if variant not in copied.variants:
        raise PackageError(f"unknown variant {variant!r}")
    definition = copied.variants[variant]
    components = _variant_paths(copied)
    for path in definition.get("dnp", []):
        components[path].fitted = False
    for path in definition.get("fit", []):
        components[path].fitted = True
    copied.parameters.update(copy.deepcopy(definition.get("parameters", {})))
    return copied


def instantiate_block(package: BlockPackage, *, instance_id: str, reference_prefix: str,
                      net_prefix: str, parameters: dict[str, Any] | None = None,
                      variant: str | None = None, shared_nets: list[str] | None = None,
                      ) -> tuple[Block, list[Connection]]:
    """Produce an isolated instance with explicit reference/net namespace prefixes.

    References are prefixed verbatim (``CH1_`` + ``R1``). Every component net and
    internal connection net is prefixed unless explicitly listed in shared_nets.
    Internal dependency paths and recognized sequencing/I2C paths are remapped.
    Returned paths start at instance_id; callers adding a parent must prepend it.
    """
    package = BlockPackage.from_dict(package.to_dict())
    _name(instance_id, "instance_id")
    _name(reference_prefix, "reference_prefix")
    _name(net_prefix, "net_prefix")
    if shared_nets is None:
        shared_nets = []
    if type(shared_nets) is not list or any(type(net) is not str or not net for net in shared_nets):
        raise PackageError("shared_nets must be an array of non-empty net names")
    if parameters is not None and type(parameters) is not dict:
        raise PackageError("instance parameters must be an object")
    root = apply_variant(package.root, variant) if variant is not None else package.root
    data = block_to_dict(root, include_frozen=False)
    old_root = root.id
    data["id"] = instance_id
    data["parameters"].update(copy.deepcopy(parameters or {}))
    shared = set(shared_nets)

    def remap_net(net: str) -> str:
        return net if net in shared else net_prefix + net

    pending = [data]
    while pending:
        block_data = pending.pop()
        block_data["depends_on"] = [_replace_path(path, old_root, instance_id) for path in block_data["depends_on"]]
        resources = block_data["resources"]
        _remap_resource_paths(resources, old_root, instance_id)
        reservations = resources.get("i2c", [])
        entries = [reservations] if type(reservations) is dict else reservations
        if type(entries) is list:
            for entry in entries:
                if type(entry) is dict and type(entry.get("bus")) is str:
                    bus = entry["bus"]
                    if not (bus == instance_id or bus.startswith(instance_id + ".")):
                        entry["bus"] = remap_net(bus)
        for component in block_data["components"]:
            component["reference"] = reference_prefix + component["reference"]
            component["pins"] = {pin: remap_net(net) for pin, net in component["pins"].items()}
        for port in block_data["ports"]:
            if "net" in port:
                port["net"] = remap_net(port["net"])
        pending.extend(block_data["children"])
    connections = [{"source": _replace_path(c.source, old_root, instance_id),
                    "target": _replace_path(c.target, old_root, instance_id), "net": remap_net(c.net)}
                   for c in package.connections]
    try:
        instance = _package_design(data, connections)
    except ModelError as exc:
        raise PackageError(f"invalid block instance: {exc}") from exc
    return instance.root, instance.connections


def instantiate_package(design: Design, parent_path: str, package: BlockPackage, **options: Any) -> Design:
    """Add a structural sketch; run preflight after connecting boundary ports."""
    from .transactions import apply_patch
    block, connections = instantiate_block(package, **options)
    old_root = block.id
    new_root = parent_path + "." + old_root
    data = block_to_dict(block)
    pending = [data]
    while pending:
        item = pending.pop()
        item["depends_on"] = [_replace_path(path, old_root, new_root) for path in item["depends_on"]]
        _remap_resource_paths(item["resources"], old_root, new_root)
        pending.extend(item["children"])
    operations = [{"op": "add_block", "parent": parent_path, "block": data}]
    operations.extend({"op": "connect", "source": _replace_path(c.source, old_root, new_root),
                       "target": _replace_path(c.target, old_root, new_root), "net": c.net} for c in connections)
    return apply_patch(design, operations, prevent_regressions=False)


@dataclass(slots=True)
class PackagePin:
    name: str
    version: str
    file: str
    sha256: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PackagePin:
        if type(data) is not dict or set(data) != {"name", "version", "file", "sha256"}:
            raise PackageError("package pins require exactly name, version, file and sha256")
        _name(data["name"], "package name")
        _version(data["version"])
        _digest(data["sha256"])
        file = data["file"]
        if (type(file) is not str or not file or "\\" in file or ":" in file
                or PurePosixPath(file).is_absolute() or any(part in ("..", ".", "") for part in file.split("/"))):
            raise PackageError("package file must be a contained relative path without traversal")
        return cls(**data)

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "version": self.version, "file": self.file, "sha256": self.sha256}


@dataclass(slots=True)
class PackageLock:
    packages: list[PackagePin] = field(default_factory=list)
    schema_version: int = 1

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PackageLock:
        if (type(data) is not dict or set(data) != {"schema_version", "packages"}
                or type(data["schema_version"]) is not int or data["schema_version"] != 1
                or type(data["packages"]) is not list):
            raise PackageError("invalid package-lock schema")
        packages = [PackagePin.from_dict(item) for item in data["packages"]]
        names = [pin.name for pin in packages]
        if len(names) != len(set(names)):
            raise PackageError("a lock must pin exactly one version per package name")
        return cls(packages)

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": self.schema_version,
                "packages": [pin.to_dict() for pin in sorted(self.packages, key=lambda p: p.name)]}


def load_lock(path: str | Path) -> PackageLock:
    try:
        return PackageLock.from_dict(read_json(path))
    except ModelError as exc:
        raise PackageError(str(exc)) from exc


def write_lock(lock: PackageLock, path: str | Path) -> None:
    lock = PackageLock.from_dict(lock.to_dict())
    atomic_write_json(lock.to_dict(), path)


def lock_package(package_path: str | Path, lock_path: str | Path) -> PackagePin:
    """Add/update a local package pin, contained within the lock directory."""
    source, lock_file = Path(package_path).resolve(), Path(lock_path)
    root = lock_file.parent.resolve()
    if not source.is_relative_to(root):
        raise PackageError("package must be within the lock directory")
    try:
        if source.stat().st_size > MAX_PACKAGE_BYTES:
            raise PackageError("package file exceeds the supported size")
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        data = read_json(source)
        package = BlockPackage.from_dict(data)
        load_package(source, expected_version=package.version, expected_sha256=digest)
        pin = PackagePin.from_dict({"name": package.name, "version": package.version,
                                    "file": source.relative_to(root).as_posix(), "sha256": digest})
        lock = load_lock(lock_file) if lock_file.exists() else PackageLock()
        lock.packages = [item for item in lock.packages if item.name != pin.name] + [pin]
        write_lock(lock, lock_file)
        return pin
    except OSError as exc:
        raise PackageError(f"cannot pin package: {exc}") from exc


def load_locked_package(lock_path: str | Path, name: str) -> BlockPackage:
    lock_file = Path(lock_path)
    lock = load_lock(lock_file)
    matches = [pin for pin in lock.packages if pin.name == name]
    if not matches:
        raise PackageError(f"package {name!r} is not pinned")
    pin = matches[0]
    root = lock_file.parent.resolve()
    path = (root / pin.file).resolve()
    if not path.is_relative_to(root):
        raise PackageError("package symlink escapes the lock directory")
    package = load_package(path, expected_version=pin.version, expected_sha256=pin.sha256)
    if package.name != pin.name:
        raise PackageError("package name differs from its lock entry")
    return package
