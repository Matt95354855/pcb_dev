"""Isolated manager workflow evaluations; never a model or hardware certificate.

Invoking this harness authorizes approval of its disposable test briefs only.
It never opens an existing user project or answers a clarification question.
Providers are injected and may use a network only under their host configuration.
"""

from __future__ import annotations

from pathlib import Path
import time

from .agent import Budget, ModelReply
from .artifacts import atomic_write, verify_manifest
from .backends.spice import generate_spice
from .checks import check_design
from .model import Design, load_design
from .project import ProjectManager
from .project_store import digest
from .transactions import atomic_write_json, read_json


CASES = {
    "diagnostic-draft": {
        "title": "Empty diagnostic draft",
        "prompt": "Test fixture only: create an EMPTY generic diagnostic PCB draft, 40 x 30 mm, two layers, manufacturer jlcpcb, with no components. First propose a brief with the supplied conditions and no invented answers; wait for test-host approval. Then propose an explicit plan, create the canonical design, run preflight and build actual preview files. No functional electronics, native validation, routing or fabrication approval is requested. All conditions are supplied; ask questions only if truly necessary.",
        "acceptance": ["explicit test-host brief approval", "plan", "40 x 30 mm two-layer empty design", "current passed metadata preflight", "verified current preview files"],
    },
    "ideal-divider": {
        "title": "Explicit ideal resistor divider",
        "prompt": "Test fixture only: create a 40 x 30 mm, two-layer jlcpcb ideal unloaded 5 V resistor divider. Exact global nets VIN, VOUT, GND. One ideal voltage source (5 V from VIN positive to GND negative), two ideal 10000-ohm resistors (VIN--VOUT and VOUT--GND); no other fitted components. Use explicit parameters.spice models V/R with value in SI and two physical pins. Root parameters vin_v=5, r_top_ohm=10000, r_bottom_ohm=10000; explicit root constraints must protect these supplied conditions. Nominal unloaded VOUT=2.5 V; physical libraries, loads, tolerances and fabrication remain unqualified. First propose a brief and wait for test-host approval. Then plan, create the design, run preflight and build actual preview files. Do not invent answers or claim native simulation/ERC/DRC occurred. No native engine run is required for this workflow test.",
        "acceptance": ["explicit test-host brief approval", "plan", "exact ideal 5 V / 10k / 10k topology", "explicit supplied-condition constraints", "current passed metadata preflight", "verified current preview files"],
    },
}


class _MeasuredProvider:
    def __init__(self, provider):
        self.provider, self.calls, self.proposed_tool_calls = provider, 0, 0
        self.network_enabled = getattr(provider, "network_enabled", None)
        self.is_demo = bool(getattr(provider, "is_demo", False))
        self.config = getattr(provider, "config", None)
        self.redaction_secrets = getattr(provider, "redaction_secrets", ())

    def complete(self, *args, **kwargs):
        self.calls += 1
        reply = self.provider.complete(*args, **kwargs)
        if isinstance(reply, ModelReply) and isinstance(reply.tool_calls, (list, tuple)):
            self.proposed_tool_calls += len(reply.tool_calls)
        return reply


def _build_matches(manager, design) -> bool:
    """Read actual verified artifacts, including the bundled canonical source."""
    for item in manager.snapshot()["artifacts"]:
        if not item["relative_path"].endswith("/build-lock.json"):
            continue
        lock_path = manager.artifact_path(item["relative_path"])
        lock = read_json(lock_path)
        if lock.get("design_sha256") != design.fingerprint() or lock.get("fabrication_ready") is not False:
            continue
        verify_manifest(lock_path.parent)
        if load_design(lock_path.parent / "design.json").fingerprint() != design.fingerprint():
            continue
        generated = lock.get("generated", {})
        required = {"board", "schematic", "netlist", "project", "rules", "manifest"}
        if not required <= set(generated):
            continue
        prefix = lock_path.parent.relative_to(manager.directory)
        paths = [manager.artifact_path((prefix / generated[key]).as_posix()) for key in required]
        if all(path.stat().st_size > 0 for path in paths):
            return True
    return False


def _case_matches(case_id, design, directory) -> bool:
    if (design.board.width_mm, design.board.height_mm, design.board.layers, design.board.manufacturer) != (40, 30, 2, "jlcpcb"):
        return False
    if case_id == "diagnostic-draft":
        return not any(block.components for _, block in design.walk_blocks())
    expected = {"vin_v": 5, "r_top_ohm": 10000, "r_bottom_ohm": 10000}
    if any(type(design.root.parameters.get(key)) not in (int, float) or design.root.parameters[key] != value for key, value in expected.items()):
        return False
    if not design.root.constraints:
        return False
    # Each fixed fixture condition must be protected by an actual constraint,
    # not merely mentioned in a tautology or in the assistant's narrative.
    for key, value in expected.items():
        mutated = Design.from_dict(design.to_dict())
        mutated.root.parameters[key] = value * 1.1
        if not any(issue.code.startswith("constraint.") for issue in check_design(mutated).issues):
            return False
    manifest = generate_spice(design, {"kind": "op"}, directory / "acceptance.cir")
    models = manifest["models"]
    resistors = [model for model in models if model["kind"] == "R"]
    sources = [model for model in models if model["kind"] == "V"]
    return (len(models) == 3 and len(resistors) == 2 and len(sources) == 1
            and sources[0]["value_si"] == 5 and sources[0]["nets"] == ["VIN", "GND"]
            and all(model["value_si"] == 10000 for model in resistors)
            and {frozenset(model["nets"]) for model in resistors} == {frozenset(("VIN", "VOUT")), frozenset(("VOUT", "GND"))})


def _assess(manager, case_id, directory, approved_hash):
    snapshot = manager.snapshot()
    checks = {"brief_approved": bool(approved_hash and (snapshot.get("brief") or {}).get("approved")
                                     and snapshot["brief"]["hash"] == approved_hash),
              "plan_present": bool(snapshot.get("plan", {}).get("steps")) if snapshot.get("plan") else False,
              "canonical_design": False, "preflight_passed": False, "actual_build_verified": False,
              "case_conditions": False, "fabrication_not_approved": snapshot.get("fabrication_ready") is False,
              "claude_unused": snapshot["usage"]["claude_tokens"] == 0 and not snapshot["claude_configured"]}
    if snapshot.get("design") is not None:
        design = Design.from_dict(snapshot["design"])
        checks["canonical_design"] = True
        fresh = check_design(design)
        stored = snapshot.get("preflight") or {}
        checks["preflight_passed"] = (fresh.passed and fresh.metadata.get("completed") is True
                                       and stored.get("passed") is True and stored.get("metadata", {}).get("design_hash") == design.fingerprint())
        checks["actual_build_verified"] = _build_matches(manager, design)
        checks["case_conditions"] = _case_matches(case_id, design, directory)
    checks["manager_completed"] = snapshot["status"] == "review_ready" and not snapshot.get("error")
    return checks


def run_evaluation(output, provider_factory, *, case_ids=None, budget=None) -> dict:
    """Run only allowlisted fixtures, creating one fresh provider/project per case.

    Calling this API is explicit test-only approval authority. UIs/CLIs must make
    that authority clear to their operator; this cannot approve a user project.
    """
    selected = list(CASES) if case_ids is None else list(case_ids)
    if (not selected or any(not isinstance(case, str) or case not in CASES for case in selected)
            or len(selected) != len(set(selected)) or not callable(provider_factory)):
        raise ValueError("Select distinct allowlisted evaluation cases and an explicit provider factory")
    budget = budget or Budget(max_steps=12, max_tokens=256_000)
    if not isinstance(budget, Budget):
        raise ValueError("Evaluation budget must be a Budget")
    destination = Path(output).absolute()
    if destination.exists() or destination.is_symlink():
        raise ValueError("Evaluation output must be a new directory")
    destination.mkdir(parents=True)
    destination = destination.resolve()
    results, providers = [], []
    for identifier in selected:
        directory = destination / "cases" / identifier
        directory.mkdir(parents=True)
        start, manager, measured = time.monotonic(), None, None
        result = {"id": identifier, "title": CASES[identifier]["title"], "passed": False,
                  "approval": None, "checks": {}, "fabrication_ready": False,
                  "case_sha256": digest(CASES[identifier]),
                  "budget": {"max_steps": budget.max_steps, "max_tokens": budget.max_tokens}}
        try:
            provider = provider_factory()
            if any(provider is prior for prior in providers):
                raise ValueError("Each case requires a fresh provider instance")
            providers.append(provider)
            measured = _MeasuredProvider(provider)
            manager = ProjectManager(directory / "project", measured, budget=budget)
            proposed = manager.send_message(CASES[identifier]["prompt"])
            brief = proposed.get("brief")
            approved_hash = None
            if proposed["status"] == "brief_proposed" and brief and not brief["questions"] and not brief["approved"] and proposed["design"] is None:
                approved_hash = brief["hash"]
                result["approval"] = {"actor": "evaluation-host", "scope": "disposable-test-fixture-only", "brief_hash": approved_hash}
                manager.approve_brief(expected_hash=approved_hash)
            result["checks"] = _assess(manager, identifier, directory, approved_hash)
            result["passed"] = all(result["checks"].values())
        except Exception as exc:
            # Never persist arbitrary exception text from a provider/factory.
            result["error"] = "Evaluation stopped: " + type(exc).__name__
        try:
            state = manager.snapshot() if manager else {}
        except Exception as exc:
            state = {}
            result.update(passed=False, error="Snapshot unavailable: " + type(exc).__name__)
        result.update(status=state.get("status", "error"), usage=state.get("usage", {}),
                      needs_input=bool((state.get("brief") or {}).get("questions")),
                      provider_type=type(providers[-1]).__name__ if measured else None,
                      provider_model=getattr(getattr(measured.config, "profile", None), "model", None) if measured else None,
                      demo=bool(measured and measured.is_demo), calls=measured.calls if measured else 0,
                      steps=measured.calls if measured else 0,
                      proposed_tool_calls=measured.proposed_tool_calls if measured else 0,
                      elapsed_seconds=round(time.monotonic() - start, 6), project_revision=state.get("revision", 0))
        results.append(result)
    report = {"schema_version": 1, "kind": "manager-workflow-evaluation", "passed": all(item["passed"] for item in results),
              "fabrication_ready": False, "model_quality_claimed": False,
              "limitations": ["Scripted/demo providers test the harness only; no Qwen score is inferred.", "Acceptance covers declared ideal metadata and verified draft files, not physical libraries, native ERC/DRC, routing, model fidelity or fabrication.", "Native engine execution is not required or inferred by these cases; no native pass is manufactured.", "Approval authorizes disposable fixture briefs only; unanswered questions are never answered automatically."],
              "summary": {"cases": len(results), "completed": sum(item["passed"] for item in results), "calls": sum(item["calls"] for item in results),
                          "qwen_tokens": sum(item["usage"].get("qwen_tokens", 0) for item in results), "unknown_qwen_calls": sum(item["usage"].get("unknown_qwen_calls", 0) for item in results)},
              "cases": results, "json_path": str(destination / "evaluation.json"), "markdown_path": str(destination / "evaluation.md")}
    atomic_write_json(report, destination / "evaluation.json")
    lines = ["# Manager workflow evaluation", "", "Disposable test fixtures only. No model qualification or fabrication approval.", "", "| Case | Completed | Model calls | Reported Qwen tokens | Unknown calls | Seconds |", "| --- | --- | ---: | ---: | ---: | ---: |"]
    for item in results:
        lines.append(f"| {item['id']} | {'yes' if item['passed'] else 'no'} | {item['calls']} | {item['usage'].get('qwen_tokens', 0)} | {item['usage'].get('unknown_qwen_calls', 0)} | {item['elapsed_seconds']} |")
    lines += ["", *["- " + limitation for limitation in report["limitations"]], ""]
    atomic_write(destination / "evaluation.md", "\n".join(lines))
    return report
