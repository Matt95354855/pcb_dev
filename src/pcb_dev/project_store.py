"""Atomic project snapshots with a verifiable local history and confined files.

Hashes detect changes relative to the initial trusted record; they are not a
signature. Token accounting deliberately lives in the independent quota ledger.
"""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import threading
from typing import Any

from .transactions import atomic_write_json, read_json


class ProjectError(ValueError):
    pass


class ProjectBusyError(ProjectError):
    pass


def clone(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False, allow_nan=False).encode("utf-8")).hexdigest()


class ProjectStore:
    """One atomic envelope contains both the audit chain and its current state."""

    def __init__(self, directory: str | Path):
        supplied = Path(directory).absolute()
        if supplied.is_symlink():
            raise ProjectError("Project directory must not be a symlink")
        supplied.mkdir(parents=True, exist_ok=True)
        if not supplied.is_dir():
            raise ProjectError("Project directory must be a directory")
        self.directory = supplied.resolve()
        self._mutex = threading.Lock()
        self.safe_path("project.json")
        self.safe_path(".project.lock")

    def safe_path(self, relative: str, *, create_parent: bool = False) -> Path:
        if (not isinstance(relative, str) or not relative or "\\" in relative
                or "\x00" in relative or ":" in relative):
            raise ProjectError("Project path must be a contained relative path")
        pure = PurePosixPath(relative)
        if pure.is_absolute() or any(part in {"", ".", ".."} for part in relative.split("/")):
            raise ProjectError("Project path escapes its directory")
        if self.directory.is_symlink() or not self.directory.is_dir():
            raise ProjectError("Project directory changed")
        current = self.directory
        for part in pure.parts:
            current = current / part
            if current.is_symlink():
                raise ProjectError("Symlinks are forbidden in project paths")
        if not current.resolve().is_relative_to(self.directory):
            raise ProjectError("Project path escapes its directory")
        if create_parent:
            current.parent.mkdir(parents=True, exist_ok=True)
        return current

    @contextmanager
    def locked(self):
        """Fail fast on a concurrent writer; OS locks are released after a crash."""
        if not self._mutex.acquire(blocking=False):
            raise ProjectBusyError("Project is busy")
        stream = None
        acquired = False
        try:
            path = self.safe_path(".project.lock")
            stream = path.open("a+b")
            if os.name == "nt":
                import msvcrt
                stream.seek(0, 2)
                if stream.tell() == 0:
                    stream.write(b"0")
                    stream.flush()
                stream.seek(0)
                try:
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError as exc:
                    raise ProjectBusyError("Project is busy") from exc
            else:
                import fcntl
                try:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise ProjectBusyError("Project is busy") from exc
            acquired = True
            yield
        finally:
            if stream is not None:
                if acquired:
                    if os.name == "nt":
                        import msvcrt
                        stream.seek(0)
                        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                stream.close()
            self._mutex.release()

    def load(self) -> dict | None:
        path = self.safe_path("project.json")
        if not path.exists():
            return None
        envelope = read_json(path)
        try:
            if set(envelope) != {"schema_version", "initial", "initial_hash", "events", "state"} or envelope["schema_version"] != 1:
                raise ProjectError("Invalid project envelope")
            state = envelope["initial"]
            previous = digest(state)
            if previous != envelope["initial_hash"] or type(envelope["events"]) is not list:
                raise ProjectError("Project initial hash is invalid")
            if state.get("revision") != 0:
                raise ProjectError("Invalid initial project revision")
            for index, event in enumerate(envelope["events"], 1):
                if set(event) != {"revision", "kind", "previous_hash", "state_hash", "hash"}:
                    raise ProjectError("Invalid project event")
                payload = {key: value for key, value in event.items() if key != "hash"}
                if (type(event["revision"]) is not int or event["revision"] != index
                        or event["previous_hash"] != previous or digest(payload) != event["hash"]
                        or not isinstance(event["kind"], str) or not event["kind"]
                        or not isinstance(event["state_hash"], str) or len(event["state_hash"]) != 64):
                    raise ProjectError("Project audit chain is invalid")
                previous = event["hash"]
            state = envelope["state"]
            expected = envelope["events"][-1]["state_hash"] if envelope["events"] else envelope["initial_hash"]
            if state.get("revision") != len(envelope["events"]) or digest(state) != expected:
                raise ProjectError("Project current state does not match its audit hash")
            if state.get("schema_version") != 1 or state.get("fabrication_ready") is not False:
                raise ProjectError("Invalid project state")
            return clone(state)
        except (TypeError, KeyError, AttributeError) as exc:
            raise ProjectError("Invalid project envelope") from exc

    def initialize(self, state: dict) -> dict:
        if self.load() is not None:
            raise ProjectError("Project already exists")
        initial = clone(state)
        initial["revision"] = 0
        initial["fabrication_ready"] = False
        envelope = {"schema_version": 1, "initial": initial,
                    "initial_hash": digest(initial), "events": [], "state": initial}
        atomic_write_json(envelope, self.safe_path("project.json"))
        return clone(initial)

    def save(self, state: dict, kind: str) -> dict:
        previous = self.load()
        if previous is None or state.get("revision") != previous["revision"]:
            raise ProjectError("Project changed; reload before saving")
        envelope = read_json(self.safe_path("project.json"))
        updated = clone(state)
        updated["revision"] += 1
        updated["fabrication_ready"] = False
        chain = envelope["events"]
        event = {"revision": updated["revision"], "kind": kind,
                 "previous_hash": chain[-1]["hash"] if chain else envelope["initial_hash"], "state_hash": digest(updated)}
        event["hash"] = digest(event)
        chain.append(event)
        envelope["state"] = updated
        if len(json.dumps(envelope, sort_keys=True, indent=2, ensure_ascii=False).encode("utf-8")) + 1 > 16_000_000:
            raise ProjectError("Project audit exceeds its bounded storage limit")
        atomic_write_json(envelope, self.safe_path("project.json"))
        return clone(updated)
