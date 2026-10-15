"""Scripted manager orchestration with real KiCad or Ngspice evidence.

The provider is deliberately scripted, not a qualified Qwen model. Engineering
tools execute normally; native failures are never converted to passes.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ..agent import Budget, ModelReply, ToolCall
from ..model import load_design
from ..project import ProjectManager


class _ScriptedProvider:
    network_enabled = False

    def __init__(self, design, mode):
        self.design, self.mode = design, mode

    def complete(self, messages, tools, *, max_tokens):
        del tools, max_tokens
        marker = "Current authoritative project snapshot: "
        state = json.loads(next(item["content"][len(marker):] for item in messages
                                if item.get("content", "").startswith(marker)))
        name, args = None, {}
        if state["brief"] is None:
            name, args = "propose_brief", {
                "title": "Scripted native orchestration fixture",
                "summary": "Exercise actual engineering engines through the persistent manager.",
                "requirements": ["Keep unrouted DRC and missing qualification blocking"],
                "assumptions": ["This provider is scripted; no model capability is measured"],
                "suggestions": [], "questions": [],
            }
        elif not state["brief"]["approved"]:
            return ModelReply("Scripted fixture awaits human brief approval.")
        elif state["plan"] is None:
            names = ["create_design", "build_draft", "native_check"] if self.mode == "kicad" else ["create_design", "simulate"]
            name, args = "propose_plan", {"steps": [
                {"id": step, "description": step, "depends_on": names[index - 1:index],
                 "tool": step, "acceptance": "Actual engine evidence, no fabrication approval"}
                for index, step in enumerate(names)]}
        elif state["design"] is None:
            name, args = "create_design", {"design": self.design}
        elif self.mode == "kicad" and not any(item.get("role") == "tool" and '"output"' in item.get("content", "") for item in messages):
            name = "build_draft"
        elif self.mode == "kicad" and state["native_evidence"] is None:
            name = "native_check"
        elif self.mode == "ngspice" and state["simulation_evidence"] is None:
            name, args = "simulate", {"analysis": self.design["metadata"]["simulation"]["analyses"]["tran"]}
        if name:
            return ModelReply("Scripted fixture: " + name, (ToolCall("fixture-" + name, name, args),))
        return ModelReply("Scripted orchestration complete. Inspect actual evidence; no fabrication approval.")


def run_smoke(output: Path, mode: str) -> dict:
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    examples = Path(__file__).resolve().parents[3] / "examples"
    design = load_design(examples / ("library_divider/port_design.json" if mode == "kicad" else "rc_filter.json")).to_dict()
    bindings = examples / "library_divider/port_bindings.json" if mode == "kicad" else None
    provider = _ScriptedProvider(design, mode)
    manager = ProjectManager(output / "project", provider, bindings=bindings, budget=Budget(max_tokens=256_000))
    result = {"status": "failed", "provider": "explicit-scripted-fixture", "fabrication_ready": False}
    try:
        proposed = manager.send_message("Run the scripted native evidence fixture.")
        if proposed["status"] != "brief_proposed" or proposed["design"] is not None:
            raise RuntimeError("Prompt-to-brief must not create a design before human approval")
        state = manager.approve_brief(expected_hash=proposed["brief"]["hash"])
        if mode == "kicad":
            evidence = state.get("native_evidence") or {}
            stages = {stage["stage"]: stage for stage in evidence.get("reports", [])}
            if any(stages.get(name, {}).get("blocking", True) for name in ("preflight", "erc", "model-parity")):
                raise RuntimeError("Actual native preflight, ERC or bound parity did not pass")
            drc = stages.get("drc", {})
            native = drc.get("metadata", {}).get("evidence", {})
            if (not drc.get("blocking") or not native.get("executed")
                    or len(native.get("native_report", {}).get("unconnected_items", [])) != 3
                    or evidence.get("validation_passed") or state["status"] != "blocked"):
                raise RuntimeError("The three real unrouted DRC blockers were not preserved")
        else:
            evidence = state.get("simulation_evidence") or {}
            if evidence.get("status") != "passed" or not evidence.get("executed"):
                raise RuntimeError("The manager did not complete real Ngspice simulation")
        resumed = ProjectManager(output / "project", provider, bindings=bindings).snapshot()
        if resumed != state or state["fabrication_ready"] or state["usage"]["qwen_tokens"]:
            raise RuntimeError("Restart changed state, scripted tokens were credited or fabrication was approved")
        result.update(status="passed", evidence=evidence, project_revision=state["revision"],
                      project_status=state["status"], restart_identical=True, artifacts=len(state["artifacts"]))
    except (ValueError, OSError, RuntimeError, KeyError, TypeError) as exc:
        result["reason"] = f"{type(exc).__name__}: {exc}"
    (output / "smoke-report.json").write_text(json.dumps(result, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--engine", choices=("kicad", "ngspice"), required=True)
    args = parser.parse_args()
    result = run_smoke(args.output, args.engine)
    print(json.dumps({key: result.get(key) for key in ("status", "provider", "fabrication_ready", "reason")}))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
