"""Restricted, reversible design transactions and a replayable audit chain.

Tool proposals operate on named model objects, never arbitrary JSON pointers or
filesystem paths.  A failed batch leaves the input design and session intact.
Audit hashes detect edits relative to a trusted initial design/event hash; they
are not signatures and cannot authenticate a wholly replaced history.
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any

from .model import Design, ModelError, canonical_json, freeze_fingerprint


class TransactionError(ModelError):
    """A proposed batch, frozen-block edit, or audit record is invalid."""


MAX_OPERATIONS = 128
_UPDATE_FIELDS = {"version", "description", "parameters", "equations", "constraints", "resources", "variants"}
_SCHEMAS = {
    "set_parameter": {"op", "block", "name", "value"},
    "add_block": {"op", "parent", "block"},
    "update_block": {"op", "block", "changes"},
    "connect": {"op", "source", "target", "net"},
    "remove_connection": {"op", "source", "target", "net"},
}


def _text(value: Any, field: str) -> str:
    if type(value) is not str or not value.strip():
        raise TransactionError(f"{field} must be a non-empty string")
    return value


def _validate_operation(operation: Any, index: int) -> dict[str, Any]:
    if type(operation) is not dict:
        raise TransactionError(f"operation {index}: expected an object")
    op = operation.get("op")
    if type(op) is not str or op not in _SCHEMAS:
        raise TransactionError(f"operation {index}: unsupported operation {op!r}")
    if set(operation) != _SCHEMAS[op]:
        raise TransactionError(f"operation {index}: {op} requires exactly {', '.join(sorted(_SCHEMAS[op]))}")
    # Also rejects non-finite data and non-JSON Python objects before copying.
    try:
        canonical_json(operation)
    except ModelError as exc:
        raise TransactionError(f"operation {index}: {exc}") from exc
    return copy.deepcopy(operation)


def _blocks(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    found = {}
    pending = [(data["root"]["id"], data["root"])]
    while pending:
        path, block = pending.pop()
        if path in found:
            raise TransactionError(f"duplicate block path {path!r}")
        found[path] = block
        for child in block.get("children", []):
            if type(child) is not dict or type(child.get("id")) is not str:
                raise TransactionError("add_block requires a block object with a string id")
            pending.append((f"{path}.{child['id']}", child))
    return found


def _assert_not_frozen(path: str, frozen: set[str]) -> None:
    for protected in frozen:
        if path == protected or path.startswith(protected + "."):
            raise TransactionError(f"{path}: cannot edit frozen block {protected!r}")


def _blocking_issues(design: Design) -> list[Any]:
    from .checks import check_design
    from .reports import Severity
    report = check_design(design)
    if report.metadata.get("completed") is not True:
        raise TransactionError("preflight could not complete; transaction cannot be checked for regressions")
    return [issue for issue in report.issues if issue.severity in (Severity.ERROR, Severity.BLOCKER)]


def _improvement(before: Any, after: Any) -> bool:
    """Permit only known monotonic improvements to an existing quantitative error."""
    if (before.code, before.block_path, before.severity) != (after.code, after.block_path, after.severity):
        return False
    old, new = dict(before.details), dict(after.details)
    directions = {
        "power.budget_exceeded": {"total_current_ma": -1, "max_current_ma": 1},
        "power.sequence_delay": {"delay_ms": 1, "required_delay_ms": -1},
        "component.rating_exceeded": {"applied": -1, "rating": 1, "derating_factor": 1},
    }.get(before.code)
    if directions is None:
        return False
    for key, direction in directions.items():
        previous, current = old.pop(key, None), new.pop(key, None)
        if type(previous) not in (int, float) or type(current) not in (int, float):
            return False
        if (current - previous) * direction < 0:
            return False
    return old == new


def _regressions(before: Design, after: Design) -> list[Any]:
    remaining = _blocking_issues(before)
    failures = []
    for issue in _blocking_issues(after):
        match = next((index for index, old in enumerate(remaining)
                      if old.fingerprint == issue.fingerprint), None)
        if match is None:
            match = next((index for index, old in enumerate(remaining) if _improvement(old, issue)), None)
        if match is None:
            failures.append(issue)
        else:
            remaining.pop(match)
    return failures


def apply_patch(design: Design, operations: list[dict[str, Any]], *,
                prevent_regressions: bool = True) -> Design:
    """Apply one atomic allowlisted batch to a validated copy of ``design``.

    Frozen subtrees and changes that invalidate their upstream context are
    always protected. The optional regression guard preserves exact blockers
    and permits known monotonic quantitative improvements; it never grants
    waivers or bypasses a manufacturing gate. Callers may explicitly construct an
    incomplete sketch with ``prevent_regressions=False``.
    """
    if type(operations) is not list or len(operations) > MAX_OPERATIONS:
        raise TransactionError(f"operations must be an array of at most {MAX_OPERATIONS} items")
    if type(prevent_regressions) is not bool:
        raise TransactionError("prevent_regressions must be a boolean")
    original = Design.from_dict(design.to_dict())
    validated_ops = [_validate_operation(operation, i) for i, operation in enumerate(operations)]
    data = original.to_dict()
    frozen = {path for path, block in original.walk_blocks() if block.frozen}
    recorded: dict[str, str] = {}
    for path, block in original.walk_blocks():
        if block.frozen:
            digest = freeze_fingerprint(original, path)
            if block.frozen_hash != digest:
                raise TransactionError(f"{path}: frozen block has no valid recorded fingerprint")
            recorded[path] = digest
    for index, operation in enumerate(validated_ops):
        try:
            op = operation["op"]
            blocks = _blocks(data)
            if op in ("set_parameter", "update_block"):
                path = _text(operation["block"], "block")
                if path not in blocks:
                    raise TransactionError(f"unknown block {path!r}")
                _assert_not_frozen(path, frozen)
                if op == "set_parameter":
                    name = _text(operation["name"], "parameter name")
                    blocks[path].setdefault("parameters", {})[name] = operation["value"]
                else:
                    changes = operation["changes"]
                    if type(changes) is not dict or not changes or set(changes) - _UPDATE_FIELDS:
                        raise TransactionError(f"changes may update only {', '.join(sorted(_UPDATE_FIELDS))}")
                    blocks[path].update(changes)
            elif op == "add_block":
                path = _text(operation["parent"], "parent")
                if path not in blocks:
                    raise TransactionError(f"unknown parent block {path!r}")
                _assert_not_frozen(path, frozen)
                new_block = operation["block"]
                if type(new_block) is not dict or type(new_block.get("id")) is not str:
                    raise TransactionError("add_block requires a block object with a string id")
                # Validate the proposed subtree separately; references can be
                # completed by later operations before the whole batch checks.
                pending = [new_block]
                while pending:
                    item = pending.pop()
                    if type(item) is not dict or item.get("frozen", False) is not False or item.get("frozen_hash") is not None:
                        raise TransactionError("a proposal cannot introduce or modify freeze records")
                    children = item.get("children", [])
                    if type(children) is not list:
                        raise TransactionError("block children must be an array")
                    pending.extend(children)
                blocks[path].setdefault("children", []).append(new_block)
            else:
                source = _text(operation["source"], "source")
                target = _text(operation["target"], "target")
                net = _text(operation["net"], "net")
                _assert_not_frozen(source.rsplit(".", 1)[0], frozen)
                _assert_not_frozen(target.rsplit(".", 1)[0], frozen)
                connection = {"source": source, "target": target, "net": net}
                if op == "connect":
                    data["connections"].append(connection)
                else:
                    matches = [i for i, item in enumerate(data["connections"])
                               if item["net"] == net and {item["source"], item["target"]} == {source, target}]
                    if len(matches) != 1:
                        raise TransactionError("remove_connection requires one existing matching connection")
                    del data["connections"][matches[0]]
        except (ModelError, KeyError, TypeError, AttributeError) as exc:
            raise TransactionError(f"operation {index}: {exc}") from exc
    try:
        candidate = Design.from_dict(data)
        for path, digest in recorded.items():
            if freeze_fingerprint(candidate, path) != digest:
                raise TransactionError(f"{path}: proposed changes invalidate a frozen block's upstream context")
        if prevent_regressions:
            new_failures = _regressions(original, candidate)
            if new_failures:
                findings = ", ".join(f"{issue.block_path or '<design>'}: {issue.code}"
                                     for issue in sorted(new_failures, key=lambda issue: (issue.block_path, issue.code)))
                raise TransactionError(f"transaction introduces blocking regressions: {findings}")
        return candidate
    except ModelError as exc:
        if isinstance(exc, TransactionError):
            raise
        raise TransactionError(f"transaction violates the design model: {exc}") from exc


def _hash(data: Any) -> str:
    return hashlib.sha256(canonical_json(data).encode("utf-8")).hexdigest()


def verify_audit(events: list[dict[str, Any]], initial_hash: str) -> str:
    """Verify a hash chain and return its final event hash (or initial hash)."""
    if type(initial_hash) is not str or len(initial_hash) != 64 or any(c not in "0123456789abcdef" for c in initial_hash):
        raise TransactionError("initial_hash must be a SHA-256 digest")
    if type(events) is not list:
        raise TransactionError("audit events must be an array")
    previous = initial_hash
    design_hash = initial_hash
    fields = {"sequence", "timestamp", "actor", "action", "before_hash", "after_hash",
              "operations", "previous_hash", "event_hash"}
    for index, event in enumerate(events):
        if type(event) is not dict or set(event) != fields:
            raise TransactionError(f"audit event {index}: invalid fields")
        if type(event["sequence"]) is not int or event["sequence"] != index + 1:
            raise TransactionError(f"audit event {index}: invalid sequence")
        if event["previous_hash"] != previous or event["before_hash"] != design_hash:
            raise TransactionError(f"audit event {index}: broken previous/design hash")
        if event["action"] not in ("apply", "undo") or type(event["operations"]) is not list:
            raise TransactionError(f"audit event {index}: invalid action or operations")
        _text(event["actor"], "audit actor")
        try:
            timestamp = datetime.fromisoformat(event["timestamp"].replace("Z", "+00:00"))
            if timestamp.tzinfo is None:
                raise ValueError("timezone missing")
        except (AttributeError, TypeError, ValueError) as exc:
            raise TransactionError(f"audit event {index}: invalid timestamp") from exc
        after_hash = event["after_hash"]
        if type(after_hash) is not str or len(after_hash) != 64 or any(c not in "0123456789abcdef" for c in after_hash):
            raise TransactionError(f"audit event {index}: invalid resulting design hash")
        expected = _hash({key: value for key, value in event.items() if key != "event_hash"})
        if event["event_hash"] != expected:
            raise TransactionError(f"audit event {index}: event hash mismatch")
        previous = expected
        design_hash = after_hash
    return previous


def atomic_write_json(data: Any, path: str | Path) -> None:
    """Replace a JSON file only after its complete bytes have reached disk."""
    destination = Path(path)
    encoded = json.dumps(data, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=destination.parent,
                                         prefix=f".{destination.name}.", suffix=".tmp", delete=False) as stream:
            temporary = stream.name
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    except OSError as exc:
        raise TransactionError(f"cannot atomically save {destination}: {exc}") from exc
    finally:
        if temporary is not None:
            try:
                Path(temporary).unlink(missing_ok=True)
            except OSError:
                pass


def read_json(path: str | Path, *, max_bytes: int = 16 * 1024 * 1024) -> Any:
    """Read data-only finite JSON while rejecting duplicate object keys."""
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result = {}
        for key, value in items:
            if key in result:
                raise TransactionError(f"duplicate JSON key {key!r}")
            result[key] = value
        return result

    def constant(value: str) -> None:
        raise TransactionError(f"non-finite JSON constant {value!r}")

    try:
        with Path(path).open("rb") as stream:
            content = stream.read(max_bytes + 1)
        if len(content) > max_bytes:
            raise TransactionError(f"JSON file exceeds {max_bytes} bytes")
        result = json.loads(content.decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant)
        canonical_json(result)
        return result
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError, ModelError) as exc:
        if isinstance(exc, TransactionError):
            raise
        raise TransactionError(f"cannot read JSON {path}: {exc}") from exc


class DesignSession:
    """An isolated editable design, one undo step per batch, and saved replay."""

    def __init__(self, design: Design, *, actor: str = "human"):
        self.actor = _text(actor, "actor")
        self._initial = Design.from_dict(design.to_dict())
        self._current = Design.from_dict(design.to_dict())
        self._undo: list[Design] = []
        self._events: list[dict[str, Any]] = []

    @property
    def design(self) -> Design:
        return Design.from_dict(self._current.to_dict())

    @property
    def history(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self._events)

    @property
    def audit_head(self) -> str:
        return verify_audit(self._events, self._initial.fingerprint())

    def _record(self, action: str, before: Design, after: Design,
                operations: list[dict[str, Any]], actor: str | None) -> None:
        event = {
            "sequence": len(self._events) + 1,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "actor": _text(self.actor if actor is None else actor, "actor"),
            "action": action, "before_hash": before.fingerprint(), "after_hash": after.fingerprint(),
            "operations": copy.deepcopy(operations), "previous_hash": self.audit_head,
        }
        event["event_hash"] = _hash(event)
        self._events.append(event)

    def apply(self, operations: list[dict[str, Any]], *, actor: str | None = None,
              prevent_regressions: bool = True) -> Design:
        if actor is not None:
            _text(actor, "actor")
        candidate = apply_patch(self._current, operations, prevent_regressions=prevent_regressions)
        self._record("apply", self._current, candidate, operations, actor)
        self._undo.append(self._current)
        self._current = candidate
        return self.design

    def undo(self, *, actor: str | None = None) -> Design:
        if not self._undo:
            raise TransactionError("there is no transaction to undo")
        if actor is not None:
            _text(actor, "actor")
        candidate = self._undo[-1]
        self._record("undo", self._current, candidate, [], actor)
        self._undo.pop()
        self._current = candidate
        return self.design

    def save(self, path: str | Path) -> None:
        self.audit_head
        atomic_write_json({"schema_version": 1, "actor": self.actor,
                           "initial_design": self._initial.to_dict(),
                           "current_design": self._current.to_dict(), "events": self.history}, path)

    @classmethod
    def load(cls, path: str | Path) -> DesignSession:
        data = read_json(path)
        fields = {"schema_version", "actor", "initial_design", "current_design", "events"}
        if type(data) is not dict or set(data) != fields or type(data["schema_version"]) is not int or data["schema_version"] != 1:
            raise TransactionError("invalid design-session schema")
        session = cls(Design.from_dict(data["initial_design"]), actor=data["actor"])
        verify_audit(data["events"], session._initial.fingerprint())
        for event in data["events"]:
            if session._current.fingerprint() != event["before_hash"]:
                raise TransactionError("session replay before-hash mismatch")
            if event["action"] == "apply":
                # Sketch transactions may intentionally add unresolved ports;
                # frozen/model protections remain enforced while replaying.
                session.apply(event["operations"], actor=event["actor"], prevent_regressions=False)
            else:
                if event["operations"]:
                    raise TransactionError("undo events cannot carry operations")
                session.undo(actor=event["actor"])
            if session._current.fingerprint() != event["after_hash"]:
                raise TransactionError("session replay after-hash mismatch")
            session._events[-1] = copy.deepcopy(event)
        expected = Design.from_dict(data["current_design"])
        if session._current.fingerprint() != expected.fingerprint():
            raise TransactionError("saved current design differs from the replayed audit")
        return session
