"""Bounded immutable canonical design snapshots and semantic comparisons.

Hashes are local integrity references, not signatures against a host administrator.
There is intentionally no restore operation in this module.
"""

from __future__ import annotations

import os
import hashlib
import tempfile
import re

from .model import Design, canonical_json
from .project_store import ProjectError, clone
from .transactions import read_json

MAX_VERSIONS = 256
MAX_VERSION_BYTES = 8_000_000
MAX_DIFF_ITEMS = 2000
MAX_DIFF_BYTES = 256_000


def require_hash(value: str) -> str:
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ProjectError("An exact SHA-256 hash is required")
    return value


def _normal(design: Design) -> dict:
    value = design.to_dict()

    def block(item):
        result = dict(item)
        result["children"] = {child["id"]: block(child) for child in item.get("children", [])}
        for key in ("components", "ports"):
            result[key] = {part["id"]: dict(part) for part in item.get(key, [])}
        for port in result["ports"].values():
            if "pin_bindings" in port:
                port["pin_bindings"] = sorted(port["pin_bindings"], key=lambda target: (target["component"], target["pin"]))
        result["depends_on"] = sorted(item.get("depends_on", []))
        return result

    value["root"] = block(value["root"])
    value["connections"] = sorted(value["connections"], key=lambda edge: (edge["source"], edge["target"], edge["net"]))
    return value


def semantic_diff(before: Design, after: Design) -> list[dict]:
    """Compare named collections by identity; never hide a truncated patch."""
    changes = []

    def pointer(value):
        return str(value).replace("~", "~0").replace("/", "~1")

    def walk(left, right, path):
        if isinstance(left, dict) and isinstance(right, dict):
            for key in sorted(left.keys() | right.keys()):
                location = path + "/" + pointer(key)
                if key not in left:
                    changes.append({"path": location, "change": "added", "before": None, "after": right[key]})
                elif key not in right:
                    changes.append({"path": location, "change": "removed", "before": left[key], "after": None})
                else:
                    walk(left[key], right[key], location)
        elif type(left) is not type(right) or left != right:
            changes.append({"path": path, "change": "changed", "before": left, "after": right})
        if len(changes) > MAX_DIFF_ITEMS:
            raise ProjectError("Design comparison exceeds the change limit; propose a smaller patch")

    walk(_normal(before), _normal(after), "")
    if len(canonical_json(changes).encode("utf-8")) > MAX_DIFF_BYTES:
        raise ProjectError("Design comparison is too large; propose a smaller patch")
    return clone(changes)


def read_version(store, state: dict, design_hash: str) -> Design:
    require_hash(design_hash)
    records = state.get("design_versions", [])
    record = next((item for item in records if item.get("design_hash") == design_hash), None)
    expected = f"versions/{design_hash}.json"
    if record is None or record.get("relative_path") != expected:
        raise ProjectError("Design version is not in this project's version manifest")
    path = store.safe_path(expected)
    if not path.is_file() or path.stat().st_size > MAX_VERSION_BYTES:
        raise ProjectError("Design version is missing or oversized")
    raw = read_json(path, max_bytes=MAX_VERSION_BYTES)
    with path.open("rb") as stream:
        content = stream.read(MAX_VERSION_BYTES + 1)
    design = Design.from_dict(raw)
    if (len(content) > MAX_VERSION_BYTES or hashlib.sha256(content).hexdigest() != design_hash
            or raw != design.to_dict() or design.fingerprint() != design_hash):
        raise ProjectError("Historical design differs from its recorded hash")
    return design


def record_version(store, state: dict, *, source: str, proposal_hash: str | None = None) -> None:
    if state.get("design") is None:
        return
    design = Design.from_dict(state["design"])
    design_hash = design.fingerprint()
    versions = state.setdefault("design_versions", [])
    if versions and versions[-1]["design_hash"] == design_hash:
        read_version(store, state, design_hash)
        return
    if len(versions) >= MAX_VERSIONS:
        raise ProjectError("Project design history reached its bounded version limit")
    relative = f"versions/{design_hash}.json"
    path = store.safe_path(relative, create_parent=True)
    content = canonical_json(design.to_dict()).encode("utf-8")
    if len(content) > MAX_VERSION_BYTES:
        raise ProjectError("Design snapshot exceeds the immutable version size limit")
    if path.exists():
        saved = Design.from_dict(read_json(path, max_bytes=MAX_VERSION_BYTES))
        if saved.fingerprint() != design_hash or path.read_bytes() != content:
            raise ProjectError("Immutable design version was changed")
    else:
        temporary = None
        try:
            descriptor, temporary = tempfile.mkstemp(prefix=".version-", dir=path.parent)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                if path.is_symlink() or path.read_bytes() != content:
                    raise ProjectError("Immutable design version already exists with different content")
            except OSError as exc:
                raise ProjectError("Filesystem cannot publish an immutable design version atomically") from exc
        finally:
            if temporary is not None:
                from pathlib import Path
                Path(temporary).unlink(missing_ok=True)
    record = {"design_hash": design_hash, "relative_path": relative,
              "revision": state["revision"] + 1, "source": source,
              "parent_hash": versions[-1]["design_hash"] if versions else None,
              "proposal_hash": proposal_hash}
    versions.append(record)
    entries = {item["relative_path"]: item for item in state["artifacts"]}
    entries[relative] = {"name": "Version du circuit " + design_hash[:12], "relative_path": relative}
    if len(entries) > 5000:
        raise ProjectError("Project artifact manifest exceeds its limit")
    state["artifacts"] = list(entries.values())


def validate_versions(store, state: dict) -> None:
    versions = state.get("design_versions", [])
    if type(versions) is not list or len(versions) > MAX_VERSIONS:
        raise ProjectError("Invalid design version manifest")
    previous_revision = -1
    previous_hash = None
    for record in versions:
        if (type(record) is not dict or set(record) != {"design_hash", "relative_path", "revision", "source", "parent_hash", "proposal_hash"}
                or type(record["revision"]) is not int or record["revision"] <= previous_revision
                or record["revision"] > state["revision"] or record["parent_hash"] != previous_hash
                or type(record["source"]) is not str or not record["source"]):
            raise ProjectError("Invalid design version history")
        if not any(item.get("relative_path") == record["relative_path"] for item in state.get("artifacts", [])):
            raise ProjectError("Historical design is missing from the artifact allowlist")
        read_version(store, state, record["design_hash"])
        if record["proposal_hash"] is not None:
            require_hash(record["proposal_hash"])
        previous_revision, previous_hash = record["revision"], record["design_hash"]
    if state.get("design") is not None and versions and Design.from_dict(state["design"]).fingerprint() != previous_hash:
        raise ProjectError("Current design differs from its latest immutable version")


def compare_versions(store, state: dict, before_hash: str, after_hash: str) -> dict:
    before, after = read_version(store, state, before_hash), read_version(store, state, after_hash)
    changes = semantic_diff(before, after)
    return {"before_hash": before_hash, "after_hash": after_hash, "changes": changes,
            "summary": {kind: sum(item["change"] == kind for item in changes) for kind in ("added", "removed", "changed")},
            "fabrication_ready": False, "read_only": True}
