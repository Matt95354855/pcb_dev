"""Real KiCad grammar/connectivity smoke, separate from mocked unit tests.

Run only in an environment with supported KiCad installed. Native unrouted DRC
findings are expected evidence; they are never converted to a fabrication pass.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import xml.etree.ElementTree as ET

from .kicad import KiCadBackend, generate


def _design(layers: int) -> dict:
    return {"schema_version": 1, "name": f"Native_Smoke_{layers}L", "version": "0.1.0",
            "board": {"width_mm": 40, "height_mm": 30, "layers": layers, "manufacturer": "jlcpcb"},
            "root": {"id": "board", "components": [
                {"id": "input", "reference": "J1", "value": "Input", "footprint": "unverified", "pins": {"1": "VIN", "2": "GND"}, "position": {"x_mm": 5, "y_mm": 5, "rotation": 0, "side": "top"}}],
                "children": [{"id": "load", "components": [
                    {"id": "resistor", "reference": "R1", "value": "1k", "footprint": "unverified", "pins": {"1": "VIN", "2": "GND"}, "position": {"x_mm": 20, "y_mm": 10, "rotation": 90, "side": "bottom"}}]}]},
            "connections": [], "metadata": {}}


def run_smoke(output_dir: Path) -> dict:
    destination = Path(output_dir).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    backend = KiCadBackend(timeout=180)
    report = {"fabrication_ready": False, "status": "failed", "diagnostic": backend.diagnose(), "designs": {}}
    try:
        if not report["diagnostic"]["supported"]:
            raise RuntimeError("Real KiCad 9 or 10 is required; this smoke cannot be mocked or skipped")
        for layers in (2, 4):
            root = destination / f"{layers}layers"
            files = generate(_design(layers), root / "source")
            item = {"netlist": backend.export_netlist(files["schematic"], root / "native.net")}
            report["designs"][str(layers)] = item
            if item["netlist"]["status"] != "passed":
                raise RuntimeError("Generated schematic could not be parsed/exported by native KiCad")
            native = ET.parse(item["netlist"]["path"])
            refs = {node.attrib["ref"] for node in native.findall("components/comp")}
            if refs != {"J1", "R1"}:
                raise RuntimeError(f"Native schematic has incorrect component references: {refs}")
            for name, pin in (("VIN", "1"), ("GND", "2")):
                node = native.find(f"nets/net[@name='{name}']")
                if node is None or {(part.attrib["ref"], part.attrib["pin"]) for part in node} != {("J1", pin), ("R1", pin)}:
                    raise RuntimeError(f"Native connectivity does not preserve global net {name}")
            item["exports"] = backend.export(files["board"], files["schematic"], root / "exports", layers=layers)
            for product in ("gerbers", "positions", "step", "schematic_svg"):
                if item["exports"]["products"][product]["status"] != "passed":
                    raise RuntimeError(f"Native {product} failed to load/export the generated files")
            # An all-SMD preview may legitimately have no drill hits/file. Its
            # export status is retained, never invented as a successful drill.
            item["checks"] = backend.check(files["board"], files["schematic"], root / "checks")
            for stage, evidence in item["checks"].items():
                if not evidence["executed"] or "native_report" not in evidence:
                    raise RuntimeError(f"Native {stage} did not produce valid report evidence")
            drc = item["checks"]["drc"]
            if drc["status"] != "failed" or not drc["native_report"]["unconnected_items"]:
                raise RuntimeError("Unrouted preview unexpectedly passed or lost native unconnected-pad findings")
            item["grammar_loaded"] = True
            item["global_nets_preserved"] = True
            item["unrouted_violations_preserved"] = True
        report["status"] = "passed"
    except (RuntimeError, OSError, ValueError, ET.ParseError) as exc:
        report["reason"] = str(exc)
    finally:
        (destination / "smoke-report.json").write_text(json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("build/native-smoke"))
    args = parser.parse_args()
    result = run_smoke(args.output)
    print(json.dumps({"status": result["status"], "fabrication_ready": False,
                      "reason": result.get("reason", "Native grammar and connectivity loaded; unrouted DRC findings were expected")}, indent=2))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
