"""Deterministic schematic sheets and bounded, contained input-closure proofs.

One file per block; electrical nets remain global. Shared-sheet instances are
deliberately unsupported. Child file UUIDs are not sheet-instance UUIDs: instance
paths use the root UUID followed by the parent sheet-object UUIDs.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import uuid

from .model import Design
from .sexpr import Quoted, parse_quoted


class HierarchyError(ValueError):
    pass


MAX_SHEETS = 256
MAX_SHEET_BYTES = 32 * 1024 * 1024
MAX_TOTAL_BYTES = 128 * 1024 * 1024
MAX_SHEET_DEPTH = 65
MAX_CHILDREN_PER_PAGE = 21
_NAMESPACE = uuid.UUID("5e64cad7-c5ec-57c7-a40a-2352e6273753")


def _uid(value: str) -> str:
    return str(uuid.uuid5(_NAMESPACE, value))


def _children(node: list, tag: str) -> list[list]:
    return [field for field in node[1:] if isinstance(field, list) and field and field[0] == tag]


def _one(node: list, tag: str) -> list:
    fields = _children(node, tag)
    if len(fields) != 1:
        raise HierarchyError(f"Expected exactly one schematic {tag} field")
    return fields[0]


@dataclass(frozen=True)
class Sheet:
    block_path: str
    parent_path: str | None
    name: str
    filename: str
    key: str
    file_uuid: str
    sheet_uuid: str
    instance_path: str
    human_path: str
    netlist_path: str
    page: str


@dataclass(frozen=True)
class SheetPlan:
    project: str
    sheets: tuple[Sheet, ...]

    def owner(self, component_path: str) -> Sheet:
        block = component_path.rsplit(".", 1)[0]
        return next(sheet for sheet in self.sheets if sheet.block_path == block)

    def children(self, parent: Sheet) -> tuple[Sheet, ...]:
        return tuple(sheet for sheet in self.sheets if sheet.parent_path == parent.block_path)

    def metadata(self) -> dict:
        return {"schema_version": 1, "mode": "hierarchical-global-nets", "project": self.project,
                "root": self.sheets[0].filename, "sheets": [asdict(sheet) for sheet in self.sheets],
                "electrical_ports": "existing global labels; no inferred hierarchical pins"}


def plan_sheets(design: Design, stem: str, root_uuid: str) -> SheetPlan:
    """Plan filenames and identities independently from output paths and offsets."""
    if not isinstance(design, Design) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", stem):
        raise HierarchyError("Hierarchy requires a canonical design and safe project stem")
    uuid.UUID(root_uuid)
    records, by_path = [], {}
    filenames, keys = set(), set()
    for path, block in design.walk_blocks():
        if len(records) >= MAX_SHEETS or len(path.split(".")) > MAX_SHEET_DEPTH:
            raise HierarchyError("Schematic hierarchy exceeds its sheet/depth limit")
        if len(block.children) > MAX_CHILDREN_PER_PAGE:
            raise HierarchyError("A block exceeds the initial 21-child A3 overview layout; group its children")
        parent_path = path.rsplit(".", 1)[0] if "." in path else None
        digest = hashlib.sha256(path.encode("utf-8")).hexdigest()[:16]
        if parent_path is None:
            filename, key = stem + ".kicad_sch", "schematic"
            sheet_uuid = file_uuid = root_uuid
            instance_path, human_path, netlist_path = "/" + root_uuid, "/", "/"
        else:
            parent = by_path[parent_path]
            filename = "sheet_" + block.id[:32] + "_" + digest + ".kicad_sch"
            key = "schematic_sheet_" + digest
            sheet_uuid, file_uuid = _uid(path + ":sheet"), _uid(path + ":file")
            instance_path = parent.instance_path + "/" + sheet_uuid
            human_path, netlist_path = parent.human_path + block.id + "/", parent.netlist_path + sheet_uuid + "/"
        if filename.casefold() in filenames or key in keys:
            raise HierarchyError("Hierarchy filenames or artifact keys collide")
        filenames.add(filename.casefold())
        keys.add(key)
        record = Sheet(path, parent_path, block.id, filename, key, file_uuid, sheet_uuid,
                       instance_path, human_path, netlist_path, str(len(records) + 1))
        records.append(record)
        by_path[path] = record
    return SheetPlan(stem, tuple(records))


def assemble_sheet(tree: list, sheet: Sheet, plan: SheetPlan) -> list:
    """Add actual child-sheet objects to a page containing only its own symbols.

    Official KiCad sheets store their parent's instance path, whereas a symbol
    stores the complete path to its containing sheet. No electrical sheet pins
    are invented: the pre-existing global labels retain all net semantics.
    """
    _one(tree, "uuid")[1] = Quoted(sheet.file_uuid)
    for symbol in _children(tree, "symbol"):
        project = _one(_one(symbol, "instances"), "project")
        project[1] = Quoted(plan.project)
        _one(project, "path")[1] = Quoted(sheet.instance_path)
    if sheet.parent_path is None:
        tree.append(["sheet_instances", ["path", Quoted("/"), ["page", Quoted(sheet.page)]]])
    tree.append(["text", Quoted("Block: " + sheet.block_path + " | Existing global nets"),
                 ["at", "12.7", "12.7", "0"], ["effects", ["font", ["size", "1.27", "1.27"]], ["justify", "left"]],
                 ["uuid", Quoted(_uid(sheet.block_path + ":heading"))]])
    for index, child in enumerate(plan.children(sheet)):
        x, y = 203.2 + (index // 7) * 76.2, 25.4 + (index % 7) * 35.56
        def coordinate(value):
            return format(value, ".12g")
        def field(name, value, ypos):
            return ["property", Quoted(name), Quoted(value), ["at", coordinate(x), coordinate(ypos), "0"],
                    ["effects", ["font", ["size", "1.27", "1.27"]], ["justify", "left"]]]
        tree.append(["sheet", ["at", coordinate(x), coordinate(y)], ["size", "60.96", "25.4"],
            ["exclude_from_sim", "no"], ["in_bom", "yes"], ["on_board", "yes"], ["dnp", "no"],
            ["stroke", ["width", "0.1524"], ["type", "solid"]], ["fill", ["color", "0", "0", "0", "0"]],
            ["uuid", Quoted(child.sheet_uuid)], field("Sheetname", child.name, y - 1.27),
            field("Sheetfile", child.filename, y + 26.67),
            ["instances", ["project", Quoted(plan.project), ["path", Quoted(sheet.instance_path),
                                                             ["page", Quoted(child.page)]]]]])
    return tree


def schematic_snapshot(source: str | Path) -> dict:
    """Snapshot every contained schematic dependency, refusing unsafe graphs.

    A flat file retains its legacy raw SHA-256. A hierarchy hashes the canonical
    sorted relative-filename/digest closure, root included. Native runners must
    compare snapshots before and after execution; hashing only the root cannot
    detect a child edit. Other engineering dependencies remain separate evidence.
    """
    supplied = Path(source).absolute()
    if supplied.is_symlink():
        raise HierarchyError("Root schematic must not be a symlink")
    base = supplied.parent.resolve()
    root = base / supplied.name
    files, uuids, spellings, inodes = {}, set(), {}, set()
    total = 0
    pending = [(root, 1)]
    while pending:
        path, depth = pending.pop()
        if depth > MAX_SHEET_DEPTH or len(files) >= MAX_SHEETS:
            raise HierarchyError("Schematic closure exceeds its file/depth limit")
        relative = path.relative_to(base).as_posix()
        if relative in files:
            raise HierarchyError("Cycles and shared schematic files are unsupported")
        if relative.casefold() in spellings:
            raise HierarchyError("Case-insensitive schematic filename collision")
        current = base
        for part in path.relative_to(base).parts:
            current /= part
            if current.is_symlink():
                raise HierarchyError("Schematic dependency or parent is a symlink")
        if not path.is_file() or not path.resolve().is_relative_to(base):
            raise HierarchyError("Schematic dependency is missing or outside its source directory")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_SHEET_BYTES:
                raise HierarchyError("Schematic dependency is empty, nonregular or oversized")
            inode = (info.st_dev, info.st_ino)
            if inode in inodes:
                raise HierarchyError("Schematic dependencies alias the same physical file")
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                raw = stream.read(MAX_SHEET_BYTES + 1)
        finally:
            os.close(descriptor)
        total += len(raw)
        if len(raw) > MAX_SHEET_BYTES or total > MAX_TOTAL_BYTES:
            raise HierarchyError("Schematic closure exceeds its bounded byte limit")
        tree = parse_quoted(raw.decode("utf-8-sig"))
        if not tree or tree[0] != "kicad_sch":
            raise HierarchyError("Schematic dependency has an invalid root")
        # Library caches may repeat definitions across files. Object identities
        # outside lib_symbols must be globally unique throughout the hierarchy.
        nodes = [tree]
        while nodes:
            node = nodes.pop()
            if not isinstance(node, list) or not node or node[0] == "lib_symbols":
                continue
            if node[0] == "uuid":
                if len(node) != 2 or not isinstance(node[1], str):
                    raise HierarchyError("Malformed schematic UUID")
                identity = str(uuid.UUID(node[1]))
                if identity in uuids:
                    raise HierarchyError("Duplicate schematic object/file UUID")
                uuids.add(identity)
            nodes.extend(field for field in node[1:] if isinstance(field, list))
        _one(tree, "uuid")
        files[relative] = hashlib.sha256(raw).hexdigest()
        spellings[relative.casefold()] = relative
        inodes.add(inode)
        child_names = set()
        for child in _children(tree, "sheet"):
            properties = [field for field in _children(child, "property") if len(field) >= 2 and field[1] == "Sheetfile"]
            if len(properties) != 1 or len(properties[0]) < 3 or not isinstance(properties[0][2], Quoted):
                raise HierarchyError("Each child sheet requires exactly one quoted Sheetfile property")
            name = str(properties[0][2])
            pure = PurePosixPath(name)
            if (not name or len(name.encode("utf-8")) > 1024 or pure.is_absolute()
                    or any(part in ("", ".", "..") for part in name.split("/"))
                    or any(char in name for char in ("\\", ":", "$", "%", "\x00"))
                    or any(ord(char) < 32 for char in name) or pure.suffix != ".kicad_sch"):
                raise HierarchyError("Child schematic filename must be a contained plain relative .kicad_sch path")
            if name.casefold() in child_names:
                raise HierarchyError("A schematic child filename is referenced more than once")
            child_names.add(name.casefold())
            _one(child, "uuid")
            pending.append((path.parent.joinpath(*pure.parts), depth + 1))
    closure = {"schema_version": 1, "root": root.name, "hierarchical": len(files) > 1,
               "files": dict(sorted(files.items()))}
    root_digest = files[root.name]
    digest = hashlib.sha256(json.dumps(closure, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest() if len(files) > 1 else root_digest
    return {"input_sha256": digest, "input_root_sha256": root_digest, "input_closure": closure}


def assert_snapshot(source: str | Path, expected: dict) -> None:
    if schematic_snapshot(source) != expected:
        raise HierarchyError("Schematic input closure changed while the native tool was running")
