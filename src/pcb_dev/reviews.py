"""Human-only decisions over bounded, validated manager patch proposals."""

from __future__ import annotations

from .checks import check_design
from .model import Design
from .project_store import ProjectError, clone, digest
from .versions import require_hash, semantic_diff

MAX_PROPOSALS = 128
MAX_PROPOSAL_BYTES = 1_000_000
_PAYLOAD = {"id", "schema_version", "base_design_hash", "base_brief_hash", "reason", "operations",
            "candidate_design_hash", "changes", "preflight_before", "preflight_after", "issue_delta"}


def pending(state: dict) -> list[dict]:
    return [item for item in state.get("patch_proposals", []) if item["status"] == "pending"]


def mark_stale(state: dict) -> None:
    design_hash = Design.from_dict(state["design"]).fingerprint() if state.get("design") else None
    brief_hash = (state.get("brief") or {}).get("hash")
    for proposal in pending(state):
        if proposal["base_design_hash"] != design_hash or proposal["base_brief_hash"] != brief_hash:
            proposal["status"] = "stale"


def validated_patch(design: Design, operations: list) -> Design:
    # Import lazily: the existing tool boundary remains the sole source of
    # operation/requirement authority for autonomous and reviewed corrections.
    from .project_tools import _component_requirements, _operations
    from .agent import _protect_requirements
    from .transactions import apply_patch

    operations = _operations(operations)
    changed = apply_patch(design, operations, prevent_regressions=True)
    _protect_requirements(design, changed)
    _component_requirements(design, changed)
    return changed


def _delta(before, after):
    first = {item["fingerprint"]: item for item in before["issues"]}
    second = {item["fingerprint"]: item for item in after["issues"]}
    return {"added": [second[key] for key in sorted(second.keys() - first.keys())],
            "resolved": [first[key] for key in sorted(first.keys() - second.keys())]}


def propose_patch(state: dict, operations: list, reason: str) -> dict:
    if pending(state):
        raise ProjectError("A patch proposal already awaits a human decision")
    proposals = state.setdefault("patch_proposals", [])
    if len(proposals) >= MAX_PROPOSALS:
        raise ProjectError("Project reached its bounded patch proposal limit")
    before = Design.from_dict(state["design"])
    after = validated_patch(before, operations)
    if after.fingerprint() == before.fingerprint():
        raise ProjectError("A patch proposal must change the canonical design")
    first, second = check_design(before).to_dict(), check_design(after).to_dict()
    payload = {"schema_version": 1, "id": f"patch-{state['revision'] + 1}-{len(proposals) + 1}",
               "base_design_hash": before.fingerprint(), "base_brief_hash": state["brief"]["hash"],
               "reason": reason, "operations": clone(operations), "candidate_design_hash": after.fingerprint(),
               "changes": semantic_diff(before, after), "preflight_before": first, "preflight_after": second,
               "issue_delta": _delta(first, second)}
    from .model import canonical_json
    if len(canonical_json(payload).encode("utf-8")) > MAX_PROPOSAL_BYTES:
        raise ProjectError("Patch proposal is too large; use a smaller correction")
    proposal = {**payload, "proposal_hash": digest(payload), "status": "pending", "decision": None}
    proposals.append(proposal)
    state["status"] = "patch_review"
    return clone(proposal)


def validate_proposals(state: dict) -> None:
    proposals = state.get("patch_proposals", [])
    if type(proposals) is not list or len(proposals) > MAX_PROPOSALS:
        raise ProjectError("Invalid patch proposal manifest")
    hashes = set()
    for item in proposals:
        if (type(item) is not dict or set(item) != _PAYLOAD | {"proposal_hash", "status", "decision"}
                or item["status"] not in {"pending", "accepted", "rejected", "stale"}):
            raise ProjectError("Invalid patch proposal")
        require_hash(item["proposal_hash"])
        if item["proposal_hash"] in hashes or digest({key: item[key] for key in _PAYLOAD}) != item["proposal_hash"]:
            raise ProjectError("Patch proposal differs from its recorded hash")
        hashes.add(item["proposal_hash"])
        for key in ("base_design_hash", "base_brief_hash", "candidate_design_hash"):
            require_hash(item[key])
        if type(item["operations"]) is not list or not 1 <= len(item["operations"]) <= 32:
            raise ProjectError("Invalid patch operation count")
        if item["status"] in {"accepted", "rejected"}:
            decision = item["decision"]
            if type(decision) is not dict or decision.get("actor") != "human" or decision.get("action") != item["status"]:
                raise ProjectError("Patch decision requires an auditable human action")
        elif item["decision"] is not None:
            raise ProjectError("Undecided patch cannot contain a human decision")
    if len(pending(state)) > 1:
        raise ProjectError("Multiple pending patch proposals are forbidden")


def proposal_for_decision(state: dict, proposal_hash: str) -> dict:
    require_hash(proposal_hash)
    validate_proposals(state)
    proposal = next((item for item in state.get("patch_proposals", []) if item["proposal_hash"] == proposal_hash), None)
    if proposal is None or proposal["status"] != "pending":
        raise ProjectError("The displayed patch is unknown, stale or already decided")
    design = Design.from_dict(state["design"])
    if (proposal["base_design_hash"] != design.fingerprint()
            or proposal["base_brief_hash"] != (state.get("brief") or {}).get("hash")
            or state.get("approved_brief_hash") != proposal["base_brief_hash"]):
        raise ProjectError("The displayed patch context changed; request a new proposal")
    return proposal


def accept_candidate(state: dict, proposal: dict) -> Design:
    before = Design.from_dict(state["design"])
    after = validated_patch(before, proposal["operations"])
    first, second = check_design(before).to_dict(), check_design(after).to_dict()
    if (after.fingerprint() != proposal["candidate_design_hash"]
            or semantic_diff(before, after) != proposal["changes"]
            or first != proposal["preflight_before"] or second != proposal["preflight_after"]):
        raise ProjectError("Patch results differ from those displayed; review a new proposal")
    return after
