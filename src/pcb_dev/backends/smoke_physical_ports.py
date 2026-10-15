"""Real KiCad/CLI gate for explicitly realized block interfaces."""

from __future__ import annotations

import argparse
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path

from ..checks import check_design
from ..cli import main as cli_main
from ..model import Design, load_design
from ..pipeline import build_preview


def run_smoke(output: Path) -> dict:
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    fixture = Path(__file__).resolve().parents[3] / "examples/library_divider"
    result = {"status": "failed", "fabrication_ready": False}
    try:
        source, bindings = fixture / "port_design.json", fixture / "port_bindings.json"
        build_preview(source, output / "draft", bindings=bindings, require_physical_ports=True)
        draft = output / "draft"
        lock = json.loads((draft / "build-lock.json").read_text(encoding="utf-8"))
        generated = lock["generated"]
        with redirect_stdout(StringIO()):
            code = cli_main(["native-check", str(source), "--bindings", str(bindings),
                "--board", str(draft / generated["board"]), "--schematic", str(draft / generated["schematic"]),
                "--require-physical-ports", "--output", str(output / "native-review"), "--json"])
        report = json.loads((output / "native-review/checks.json").read_text(encoding="utf-8"))
        stages = {stage["stage"]: stage for stage in report["reports"]}
        preflight, erc, drc, parity = [stages[name] for name in ("preflight", "erc", "drc", "model-parity")]
        coverage = preflight["metadata"]["physical_ports"]
        if not coverage["complete"] or coverage["realized_ports"] != 4:
            raise RuntimeError("The four declared block interfaces were not physically realized")
        for name, stage in (("preflight", preflight), ("erc", erc), ("parity", parity)):
            if stage["blocking"] or not stage["metadata"].get("completed"):
                raise RuntimeError(f"Required {name} evidence failed or is incomplete")
        native_erc = erc["metadata"]["evidence"]
        native_drc = drc["metadata"]["evidence"]
        if not native_erc["executed"] or native_erc["status"] != "passed" or not native_erc.get("native_report"):
            raise RuntimeError("ERC was not actually executed and passed")
        findings = native_drc.get("native_report", {})
        if (code != 1 or report["validation_passed"] or not drc["blocking"]
                or not native_drc["executed"] or native_drc["status"] != "failed"
                or len(findings.get("unconnected_items", [])) != 3 or findings.get("ignored_checks")):
            raise RuntimeError("The three native unrouted blockers must remain blocking")
        design = load_design(source)
        wrong = Design.from_dict(design.to_dict())
        wrong.root.children[1].components[0].pins["1"] = "GND"
        rejected = check_design(wrong, require_physical_ports=True)
        if not rejected.blocking:
            raise RuntimeError("Physical supply pin moved to GND without a blocking preflight")
        result.update(status="passed", physical_ports=coverage,
                      native_erc_passed=True, native_bound_parity_passed=True,
                      unrouted_drc_blocked=True, incorrect_pin_mapping_rejected=True,
                      native_review=report, intentional_mismatch=rejected.to_dict())
    except (ValueError, OSError, RuntimeError, KeyError, TypeError) as exc:
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
