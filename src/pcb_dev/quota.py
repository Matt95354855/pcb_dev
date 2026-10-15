"""Persistent project token accounting; Claude needs human authority and Q/9 credit.

API-reported counters and conservative reservations are separate quantities.
Uncertain requests retain their reservation and block further Claude calls.
This local policy is not a server billing guarantee and cannot undo a request.
"""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import stat
import tempfile
import uuid


class QuotaError(ValueError):
    pass


_MAX_LEDGER_BYTES = 16 * 1024 * 1024
_COUNTERS = ("qwen_input_tokens", "qwen_output_tokens", "qwen_calls", "unknown_qwen_calls",
             "claude_input_tokens", "claude_output_tokens", "claude_cache_read_input_tokens",
             "claude_cache_creation_input_tokens")
_USAGE = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")


def _integer(value, name):
    if type(value) is not int or not 0 <= value <= 10**18:
        raise QuotaError(f"{name} must be a nonnegative bounded integer")
    return value


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise QuotaError("Duplicate token-ledger field")
        result[key] = value
    return result


def _reject_constant(_):
    raise QuotaError("Nonfinite token-ledger value")


class TokenLedger:
    """Use a dedicated file outside design snapshots, rollback and checkpoints.

    Every operation locks and rereads disk, including between distinct instances.
    There is deliberately no reset, cancellation refund or automatic unquarantine.
    A pending reservation also blocks another review: after a process crash it
    remains charged until a separate human reconciliation process is implemented.
    """

    def __init__(self, project_dir: str | Path):
        directory = Path(project_dir).absolute()
        if directory.is_symlink():
            raise QuotaError("Token ledger project directory cannot be a symlink")
        directory.mkdir(parents=True, exist_ok=True)
        self.directory = directory.resolve()
        self.path = self.directory / "token-ledger.json"
        self.lock_path = self.directory / ".token-ledger.lock"
        with self._locked():
            if not self.path.exists() and not self.path.is_symlink():
                self._write({"schema_version": 1, **dict.fromkeys(_COUNTERS, 0),
                             "claude_quarantined": False, "reservations": {}})
            self._read()

    @contextmanager
    def _locked(self):
        if self.lock_path.is_symlink():
            raise QuotaError("Token-ledger lock cannot be a symlink")
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.lock_path, flags, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise QuotaError("Token-ledger lock must be a regular file")
            if os.name == "nt":
                import msvcrt
                if os.fstat(descriptor).st_size == 0:
                    os.write(descriptor, b"\0")
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
                try:
                    yield
                finally:
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                yield
        finally:
            os.close(descriptor)

    def _read(self):
        if self.path.is_symlink() or not self.path.is_file() or not 0 < self.path.stat().st_size <= _MAX_LEDGER_BYTES:
            raise QuotaError("Token ledger is missing, linked, empty or oversized")
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"), object_pairs_hook=_pairs,
                              parse_constant=_reject_constant)
        except (ValueError, UnicodeError, RecursionError):
            raise QuotaError("Token ledger is corrupt; it must not be reset automatically") from None
        fields = {"schema_version", "claude_quarantined", "reservations", *_COUNTERS}
        if (type(data) is not dict or set(data) != fields or type(data["schema_version"]) is not int
                or data["schema_version"] != 1 or type(data["claude_quarantined"]) is not bool
                or type(data["reservations"]) is not dict):
            raise QuotaError("Token ledger schema is invalid")
        for name in _COUNTERS:
            _integer(data[name], name)
        if data["unknown_qwen_calls"] > data["qwen_calls"]:
            raise QuotaError("Unknown Qwen calls contradict the call count")
        totals = dict.fromkeys(_USAGE, 0)
        for identifier, reservation in data["reservations"].items():
            if (not isinstance(identifier, str) or len(identifier) != 32 or type(reservation) is not dict
                    or set(reservation) != {"tokens", "status", "usage"}
                    or reservation["status"] not in ("pending", "uncertain", "settled")
                    or type(reservation["usage"]) is not dict
                    or set(reservation["usage"]) not in (set(), set(_USAGE))):
                raise QuotaError("Token reservation record is invalid")
            if _integer(reservation["tokens"], "reserved tokens") == 0:
                raise QuotaError("Token reservation must be positive")
            if (reservation["status"] == "settled" and set(reservation["usage"]) != set(_USAGE)
                    or reservation["status"] == "pending" and reservation["usage"]):
                raise QuotaError("Token reservation state contradicts its usage")
            for name, value in reservation["usage"].items():
                totals[name] += _integer(value, name)
        if any(data["claude_" + name] != total for name, total in totals.items()):
            raise QuotaError("Token ledger totals contradict recorded API usage")
        return data

    def _write(self, data):
        for name in _COUNTERS:
            _integer(data[name], name)
        encoded = (json.dumps(data, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()
        if len(encoded) > _MAX_LEDGER_BYTES:
            raise QuotaError("Token ledger exceeds its size bound")
        descriptor, temporary = tempfile.mkstemp(prefix=".token-ledger-", dir=self.directory)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            if os.name != "nt":
                directory_fd = os.open(self.directory, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    @staticmethod
    def _snapshot(data):
        qwen = data["qwen_input_tokens"] + data["qwen_output_tokens"]
        claude = sum(data["claude_" + name] for name in _USAGE)
        unsettled = [record for record in data["reservations"].values() if record["status"] != "settled"]
        reserved = sum(record["tokens"] for record in unsettled)
        quarantined = data["claude_quarantined"] or bool(unsettled)
        return {"qwen_tokens": qwen, "claude_tokens": claude,
            "qwen_input_tokens": data["qwen_input_tokens"], "qwen_output_tokens": data["qwen_output_tokens"],
            "claude_input_tokens": data["claude_input_tokens"], "claude_output_tokens": data["claude_output_tokens"],
            "claude_cache_read_input_tokens": data["claude_cache_read_input_tokens"],
            "claude_cache_creation_input_tokens": data["claude_cache_creation_input_tokens"],
            "claude_reserved_tokens": reserved, "claude_policy_limit_tokens": qwen // 9,
            "claude_available_tokens": 0 if quarantined else max(0, qwen // 9 - claude - reserved),
            "claude_share": claude / (qwen + claude) if qwen + claude else 0.0,
            "unknown_qwen_calls": data["unknown_qwen_calls"], "qwen_calls": data["qwen_calls"],
            "unknown_claude_calls": len(unsettled), "claude_quarantined": quarantined,
            "quota_verified": not quarantined and claude * 9 <= qwen,
            "policy": "reported-project-tokens; Claude <= floor(Qwen/9); uncertain reservations retained"}

    def snapshot(self) -> dict:
        with self._locked():
            return self._snapshot(self._read())

    def record_qwen(self, input_tokens: int, output_tokens: int, usage_reported: bool) -> None:
        if type(usage_reported) is not bool:
            raise QuotaError("usage_reported must be a boolean")
        if usage_reported:
            _integer(input_tokens, "Qwen input tokens")
            _integer(output_tokens, "Qwen output tokens")
        with self._locked():
            data = self._read()
            data["qwen_calls"] += 1
            if usage_reported:
                data["qwen_input_tokens"] += input_tokens
                data["qwen_output_tokens"] += output_tokens
            else:
                data["unknown_qwen_calls"] += 1
            self._write(data)

    def reserve_claude(self, tokens: int, *, human_authorized: bool) -> str:
        if human_authorized is not True:
            raise QuotaError("Claude requires explicit human authorization for this review")
        if _integer(tokens, "Claude reservation") == 0:
            raise QuotaError("Claude reservation must be positive")
        with self._locked():
            data = self._read()
            snapshot = self._snapshot(data)
            if snapshot["claude_quarantined"]:
                raise QuotaError("Claude is blocked by an active or uncertain project reservation")
            if tokens > snapshot["claude_available_tokens"]:
                raise QuotaError("Claude reservation exceeds the project's reported-token 10% envelope")
            identifier = uuid.uuid4().hex
            data["reservations"][identifier] = {"tokens": tokens, "status": "pending", "usage": {}}
            self._write(data)
            return identifier

    def settle_claude(self, identifier: str, *, input_tokens: int, output_tokens: int,
                      cache_read_input_tokens: int = 0, cache_creation_input_tokens: int = 0,
                      usage_reported: bool = True) -> dict:
        usage = {"input_tokens": input_tokens, "output_tokens": output_tokens,
                 "cache_read_input_tokens": cache_read_input_tokens,
                 "cache_creation_input_tokens": cache_creation_input_tokens}
        valid = usage_reported is True and all(type(value) is int and 0 <= value <= 10**18 for value in usage.values())
        with self._locked():
            data = self._read()
            record = data["reservations"].get(identifier)
            if record is None:
                raise QuotaError("Unknown Claude reservation")
            if record["status"] == "settled" or record["usage"]:
                if valid and record["usage"] == usage:
                    return self._snapshot(data)
                raise QuotaError("Claude reservation cannot be settled twice with different usage")
            if valid:
                record["usage"] = usage
                for name, value in usage.items():
                    data["claude_" + name] += value
            if not valid or sum(usage.values()) > record["tokens"]:
                record["status"] = "uncertain"
                data["claude_quarantined"] = True
            else:
                record["status"] = "settled"
            self._write(data)
            return self._snapshot(data)

    def fail_claude(self, identifier: str, reason: str = "uncertain request") -> dict:
        # Do not persist arbitrary exception/reason text: it can echo an API
        # credential, a prompt, headers or an upstream response body.
        with self._locked():
            data = self._read()
            record = data["reservations"].get(identifier)
            if record is None:
                raise QuotaError("Unknown Claude reservation")
            record["status"] = "uncertain"
            data["claude_quarantined"] = True
            self._write(data)
            return self._snapshot(data)
