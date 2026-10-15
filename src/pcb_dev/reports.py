"""Deterministic findings, explicitly attributed waivers, and fail-closed gates.

A clean Python preflight is evidence about the metadata supplied to pcb-dev. It
is never evidence that KiCad ERC or DRC ran. ``gate_reports`` requires separate,
completed, native reports for these stages and preserves every failure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import hashlib
import html
import json
from typing import Any, Iterable, Mapping


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    BLOCKER = "blocker"


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _safe_text(value: Any) -> str:
    """Keep untrusted values within their Markdown table cell."""
    return html.escape(str(value), quote=False).replace("|", "\\|").replace("\r", " ").replace("\n", "<br>")


@dataclass(frozen=True)
class Issue:
    code: str
    severity: Severity | str
    message: str
    block_path: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "severity", Severity(self.severity))
        if not isinstance(self.code, str) or not self.code.strip():
            raise ValueError("An issue requires a nonempty code")
        if not isinstance(self.message, str) or not self.message.strip():
            raise ValueError("An issue requires a nonempty message")
        if not isinstance(self.block_path, str) or not isinstance(self.details, dict):
            raise ValueError("Invalid issue path or details")
        # Copy nested containers: later changes by a caller must not change a
        # finding's identity after a human has approved a waiver for it.
        object.__setattr__(self, "details", json.loads(_json(self.details)))

    @property
    def fingerprint(self) -> str:
        payload = {"code": self.code, "severity": self.severity.value,
                   "message": self.message, "block_path": self.block_path,
                   "details": self.details}
        return hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "severity": self.severity.value,
                "message": self.message, "block_path": self.block_path,
                "details": json.loads(_json(self.details)), "fingerprint": self.fingerprint}


@dataclass(frozen=True)
class Waiver:
    issue_fingerprint: str
    design_hash: str
    actor: str
    reason: str
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    human_approved: bool = False

    def __post_init__(self) -> None:
        for name in ("issue_fingerprint", "design_hash"):
            value = getattr(self, name)
            if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
                raise ValueError(f"{name} must be an exact SHA-256 hexadecimal fingerprint")
        for name in ("actor", "reason"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name).strip():
                raise ValueError(f"A waiver requires a nonempty {name}")
        if type(self.human_approved) is not bool:
            raise ValueError("human_approved must be a boolean")
        try:
            timestamp = datetime.fromisoformat(self.created_at.replace("Z", "+00:00"))
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("created_at must be an ISO-8601 timestamp") from exc
        if timestamp.tzinfo is None:
            raise ValueError("created_at must include its timezone")

    def to_dict(self) -> dict[str, Any]:
        return {"issue_fingerprint": self.issue_fingerprint, "design_hash": self.design_hash,
                "actor": self.actor, "reason": self.reason, "created_at": self.created_at,
                "human_approved": self.human_approved}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Waiver":
        allowed = {"issue_fingerprint", "design_hash", "actor", "reason", "created_at", "human_approved"}
        if set(data) - allowed:
            raise ValueError("Unknown waiver fields")
        return cls(**dict(data))


# Missing proof is never a finding that a waiver can turn into successful proof.
_NON_WAIVABLE = {"gate.missing_stage", "gate.incomplete_stage", "gate.not_native",
                 "gate.stale_report", "gate.duplicate_stage", "gate.stage_failed"}


@dataclass
class CheckReport:
    stage: str
    issues: list[Issue] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    waivers: list[Waiver] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not isinstance(self.stage, str) or not self.stage:
            raise ValueError("A report requires a stage name")
        self.issues = list(self.issues)
        self.metadata = json.loads(_json(self.metadata))
        if not all(isinstance(issue, Issue) for issue in self.issues):
            raise ValueError("Report issues must be Issue instances")
        if not all(isinstance(waiver, Waiver) for waiver in self.waivers):
            raise ValueError("Report waivers must be Waiver instances")

    def add(self, issue: Issue) -> None:
        if not isinstance(issue, Issue):
            raise TypeError("Expected an Issue")
        self.issues.append(issue)

    def is_waived(self, issue: Issue) -> bool:
        design_hash = self.metadata.get("design_hash")
        return issue.code not in _NON_WAIVABLE and any(
            waiver.human_approved is True and waiver.issue_fingerprint == issue.fingerprint
            and waiver.design_hash == design_hash for waiver in self.waivers)

    @property
    def blocking(self) -> bool:
        return self.metadata.get("completed") is False or any(
            issue.severity in (Severity.ERROR, Severity.BLOCKER) and not self.is_waived(issue)
            for issue in self.issues)

    @property
    def passed(self) -> bool:
        return not self.blocking

    def apply_waivers(self, waivers: Iterable[Waiver], design_hash: str | None = None) -> "CheckReport":
        """Return an audited copy; unmatched/unapproved waivers remain visible.

        The report's own design hash is authoritative. A different supplied hash
        raises rather than silently rebinding previously generated evidence.
        """
        metadata = dict(self.metadata)
        if design_hash is not None:
            if metadata.get("design_hash") not in (None, design_hash):
                raise ValueError("Cannot apply waivers to a report for a different design")
            metadata["design_hash"] = design_hash
        return CheckReport(self.stage, self.issues, metadata, [*self.waivers, *waivers])

    def to_dict(self) -> dict[str, Any]:
        findings = []
        for issue in self.issues:
            item = issue.to_dict()
            item["waived"] = self.is_waived(issue)
            findings.append(item)
        return {"stage": self.stage, "passed": self.passed, "blocking": self.blocking,
                "issues": findings, "metadata": json.loads(_json(self.metadata)),
                "waivers": [waiver.to_dict() for waiver in self.waivers]}

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, indent=indent, allow_nan=False) + "\n"

    def to_markdown(self) -> str:
        status = "BLOCKED" if self.blocking else "PASS"
        lines = [f"# {_safe_text(self.stage)}: {status}", ""]
        if self.metadata.get("design_hash"):
            lines += [f"Design SHA-256: `{self.metadata['design_hash']}`", ""]
        if not self.issues:
            lines += ["No findings reported for this stage.", ""]
        else:
            lines += ["| Severity | Code | Location | Finding | Waived |", "| --- | --- | --- | --- | --- |"]
            for issue in self.issues:
                lines.append("| " + " | ".join(_safe_text(v) for v in (
                    issue.severity.value, issue.code, issue.block_path, issue.message,
                    "yes" if self.is_waived(issue) else "no")) + " |")
            lines.append("")
        if self.waivers:
            lines += ["## Waiver audit", "", "| Issue fingerprint | Actor | Reason | Timestamp | Human approved | Applied |",
                      "| --- | --- | --- | --- | --- | --- |"]
            for waiver in self.waivers:
                matched = any(issue.fingerprint == waiver.issue_fingerprint and self.is_waived(issue)
                              and waiver.human_approved and waiver.design_hash == self.metadata.get("design_hash")
                              for issue in self.issues)
                lines.append("| " + " | ".join(_safe_text(v) for v in (
                    waiver.issue_fingerprint, waiver.actor, waiver.reason, waiver.created_at,
                    "yes" if waiver.human_approved else "no", "yes" if matched else "no")) + " |")
            lines.append("")
        lines += ["Stage evidence:", "", "```json", json.dumps(self.metadata, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False), "```", ""]
        return "\n".join(lines)

    # Convenient spelling for callers which render reports directly.
    markdown = to_markdown


def gate_reports(reports: Iterable[CheckReport], *, required_stages: Iterable[str] = ("preflight", "erc", "drc"),
                 design_hash: str | None = None) -> CheckReport:
    """Require completed reports and native ERC/DRC evidence, failing closed."""
    reports = list(reports)
    required_stages = tuple(required_stages)
    metadata: dict[str, Any] = {"completed": True, "required_stages": list(required_stages),
                               "reports": [report.to_dict() for report in reports]}
    if design_hash is not None:
        metadata["design_hash"] = design_hash
    gate = CheckReport("release-gate", metadata=metadata)
    for stage in required_stages:
        matches = [report for report in reports if report.stage == stage]
        if not matches:
            gate.add(Issue("gate.missing_stage", Severity.BLOCKER, f"Required stage {stage} has no report.", stage))
            continue
        if len(matches) != 1:
            gate.add(Issue("gate.duplicate_stage", Severity.BLOCKER, f"Stage {stage} has ambiguous duplicate reports.", stage))
        for report in matches:
            if report.metadata.get("completed") is not True:
                gate.add(Issue("gate.incomplete_stage", Severity.BLOCKER, f"Stage {stage} did not complete.", stage))
            if stage in ("erc", "drc") and report.metadata.get("native") is not True:
                gate.add(Issue("gate.not_native", Severity.BLOCKER, f"Stage {stage} has no native KiCad execution evidence.", stage))
            if design_hash is not None and report.metadata.get("design_hash") != design_hash:
                gate.add(Issue("gate.stale_report", Severity.BLOCKER, f"Stage {stage} does not match the current design.", stage))
            if report.blocking:
                gate.add(Issue("gate.stage_failed", Severity.BLOCKER, f"Stage {stage} has unresolved blocking findings.", stage,
                               {"blocking_codes": [issue.code for issue in report.issues
                                                   if issue.severity in (Severity.ERROR, Severity.BLOCKER)
                                                   and not report.is_waived(issue)]}))
    # Optional supplied reports are evidence too; a failed extra stage cannot be
    # hidden just by omitting its name from the required list.
    for report in reports:
        if report.stage not in required_stages and report.blocking:
            gate.add(Issue("gate.stage_failed", Severity.BLOCKER,
                           f"Supplied stage {report.stage} has unresolved blocking findings.", report.stage))
    return gate
