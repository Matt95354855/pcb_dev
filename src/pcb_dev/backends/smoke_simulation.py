"""Real Ngspice RC smoke with waveform and independently evaluated measurement."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .simulation import NgSpiceBackend
from .spice import SpiceError, generate_spice
from ..model import load_design


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("build/native-ngspice"))
    output = parser.parse_args().output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    circuit = output / "rc.cir"
    circuit.write_text("RC first-order transient\nV1 in 0 DC 5\nR1 in out 1k\nC1 out 0 1u\n.ic v(out)=0\n.tran 10u 10m uic\n.meas tran vfinal FIND v(out) AT=10m\n.end\n", encoding="utf-8")
    result = NgSpiceBackend().simulate(circuit, output / "results")
    result["acceptance"] = {"measurement": "vfinal", "min": 4.99, "max": 5.01,
                            "passed": result["status"] == "passed" and 4.99 <= result["measurements"].get("vfinal", -1) <= 5.01}
    result["generated_fixtures"] = {}
    try:
        examples = Path(__file__).resolve().parents[3] / "examples"
        cases = [("rc_filter", "tran", "vfinal", "tran", 0.01, 4.99, 5.01),
                 ("voltage_divider", "dc", "divider_vout", "dc", 5, 2.499, 2.501)]
        for fixture, analysis_name, measurement, mode, at_value, minimum, maximum in cases:
            design = load_design(examples / (fixture + ".json"))
            folder = output / fixture
            generated = generate_spice(design, design.metadata["simulation"]["analyses"][analysis_name], folder / "generated.cir", ground_nets=["GND"])
            node = generated["net_map"]["VOUT"]
            measured = folder / "measured.cir"
            measured.write_text(Path(generated["path"]).read_text().replace(".end\n", f".meas {mode} {measurement} FIND v({node}) AT={at_value}\n.end\n"), encoding="utf-8")
            native = NgSpiceBackend().simulate(measured, folder / "results")
            accepted = native["status"] == "passed" and minimum <= native["measurements"].get(measurement, -1) <= maximum
            result["generated_fixtures"][fixture] = {"generation": generated, "simulation": native,
                                                    "acceptance": {"measurement": measurement, "min": minimum, "max": maximum, "passed": accepted}}
    except (OSError, ValueError, SpiceError) as exc:
        result["fixture_error"] = str(exc)
    passed = result["acceptance"]["passed"] and len(result["generated_fixtures"]) == 2 and all(case["acceptance"]["passed"] for case in result["generated_fixtures"].values())
    result["status"] = "passed" if passed else "failed"
    (output / "smoke-report.json").write_text(json.dumps(result, sort_keys=True, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({"status": "passed" if passed else "failed",
                      "vfinal": result["measurements"].get("vfinal"), "reason": result.get("reason", "")}, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
