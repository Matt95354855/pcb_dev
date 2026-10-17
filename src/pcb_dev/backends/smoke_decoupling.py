"""Real KiCad fixture with pinned capacitor pads and declared decoupling rules.

J2 is an ideal test-load adapter, not a qualified MCU. The component values and
distance limit are disposable fixture assumptions, never datasheet conclusions.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ..bindings import load_bindings
from ..checks import check_design
from ..decoupling import check_layout
from ..model import Design, load_design, walk_components
from ..native_validation import run_native_validation
from ..pipeline import build_preview
from .library_kicad import generate_bound


def run_smoke(output: Path) -> dict:
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    fixture = Path(__file__).resolve().parents[3] / "examples/decoupling"
    result = {"status": "failed", "fabrication_ready": False, "fixture_only": True}
    try:
        source, bindings = fixture / "design.json", fixture / "bindings.json"
        design = load_design(source)
        draft = output / "draft"
        build_preview(source, draft, bindings=bindings, require_physical_ports=True, hierarchical=True)
        lock = json.loads((draft / "build-lock.json").read_text(encoding="utf-8"))
        generated = lock["generated"]
        bundled = draft / lock["bundled_bindings"]
        report, _ = run_native_validation(design, draft / generated["board"], draft / generated["schematic"],
                                         output / "native-review", bindings=bundled, require_physical_ports=True)
        stages = {stage["stage"]: stage for stage in report["reports"]}
        for name in ("preflight", "erc", "model-parity", "decoupling-layout"):
            stage = stages[name]
            if stage["blocking"] or not stage["metadata"].get("completed"):
                raise RuntimeError(f"Required {name} evidence failed or is incomplete")
        if not stages["erc"]["metadata"]["evidence"].get("executed"):
            raise RuntimeError("Native ERC was not actually executed")
        layout = stages["decoupling-layout"]
        measurements = layout["metadata"]["measurements"]
        if (not layout["metadata"].get("distance_verified") or len(measurements) != 1
                or abs(measurements[0]["distance_mm"] - 1.0) > 1e-6
                or layout["metadata"].get("source_verified") is not False):
            raise RuntimeError("Actual pad proximity or declared-source scope was not preserved")
        drc = stages["drc"]["metadata"]["evidence"]
        if (report["validation_passed"] or not stages["drc"]["blocking"] or not drc.get("executed")
                or drc.get("status") != "failed" or not drc.get("native_report", {}).get("unconnected_items")):
            raise RuntimeError("The real unrouted DRC must remain blocking")

        # A far-away capacitor can have a healthy metadata preflight. The actual
        # pinned pad geometry must independently reject its new PCB placement.
        far = Design.from_dict(design.to_dict())
        capacitor = next(component for _, component in walk_components(far) if component.reference == "C1")
        capacitor.position.x_mm += 15
        if check_design(far).blocking:
            raise RuntimeError("The distance mutation unexpectedly changed electrical intent")
        far_bound = load_bindings(far, bindings)
        far_files = generate_bound(far_bound, output / "far-pads", hierarchical=True)
        far_report = check_layout(far, far_files["board"], far_bound)
        if not far_report.blocking or not any(issue.code == "decoupling.distance" for issue in far_report.issues):
            raise RuntimeError("Far-away actual capacitor pads did not block the layout rule")
        (output / "far-pads-check.json").write_text(far_report.to_json() + "\n", encoding="utf-8")
        result.update(status="passed", native_review=report, nominal_distance_mm=1.0,
                      native_erc_passed=True, bound_parity_passed=True, unrouted_drc_blocked=True,
                      far_pad_distance_rejected=True, source_verified=False)
    except (ValueError, OSError, RuntimeError, KeyError, TypeError, StopIteration) as exc:
        result["reason"] = f"{type(exc).__name__}: {exc}"
    (output / "smoke-report.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    result = run_smoke(parser.parse_args().output)
    print(json.dumps({"status": result["status"], "fabrication_ready": False, "reason": result.get("reason")}))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
