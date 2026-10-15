"""Deterministic SPICE generation from explicit two-terminal component models.

No behaviour is inferred from a display value, footprint, component prefix, or
block name. Unsupported fitted parts fail before any files are written.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Sequence

from ..model import Design, ModelError, walk_components


class SpiceError(ValueError):
    """A design or requested analysis cannot be represented safely in SPICE."""


_SCALES = {"": 1, "f": 1e-15, "p": 1e-12, "n": 1e-9, "u": 1e-6,
           "m": 1e-3, "k": 1e3, "K": 1e3, "M": 1e6, "meg": 1e6,
           "Meg": 1e6, "MEG": 1e6, "G": 1e9, "T": 1e12}
_VALUE = re.compile(r"([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)(MEG|Meg|meg|[fpnumkKMGT])?")
_KINDS = frozenset({"R", "C", "L", "V", "I"})
_COMMON_ANALYSIS = frozenset({"kind", "reltol", "abstol_a", "vntol_v"})


def _number(value, location: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SpiceError(f"{location}: a finite numeric value is required")
    try:
        number = float(value)
    except (OverflowError, ValueError) as exc:
        raise SpiceError(f"{location}: number is outside the supported range") from exc
    if not math.isfinite(number) or (positive and number <= 0):
        raise SpiceError(f"{location}: a finite {'positive ' if positive else ''}number is required")
    return number


def _value(value, location: str, *, positive: bool) -> float:
    if not isinstance(value, str):
        return _number(value, location, positive=positive)
    match = _VALUE.fullmatch(value)
    if not match:
        raise SpiceError(f"{location}: expected a number or strict SI literal, without units/cards")
    try:
        decimal = Decimal(match[1]) * Decimal(str(_SCALES[match[2] or ""]))
        number = float(decimal)
    except (InvalidOperation, OverflowError, ValueError) as exc:
        raise SpiceError(f"{location}: invalid or out-of-range SI literal") from exc
    if decimal != 0 and number == 0:
        raise SpiceError(f"{location}: SI literal underflows the supported numeric range")
    return _number(number, location, positive=positive)


def _literal(number: float) -> str:
    return repr(number) if number != 0 else "0"


def _object(value, location: str, allowed: set | frozenset, required: set | frozenset) -> dict:
    if not isinstance(value, dict):
        raise SpiceError(f"{location}: expected an object")
    if set(value) - allowed or required - set(value):
        raise SpiceError(f"{location}: unknown or missing fields (allowed: {', '.join(sorted(allowed))})")
    return value


def _analysis(raw: dict, models: list[dict]) -> tuple[dict, list[str]]:
    if not isinstance(raw, dict) or not isinstance(raw.get("kind"), str):
        raise SpiceError("analysis.kind must be op, tran, ac, or dc")
    kind = raw["kind"]
    extras = {"op": set(), "tran": {"step_s", "stop_s", "start_s", "max_step_s", "uic"},
              "ac": {"scale", "points", "start_hz", "stop_hz"},
              "dc": {"source", "start", "stop", "step"}}
    if kind not in extras:
        raise SpiceError("analysis.kind must be op, tran, ac, or dc")
    _object(raw, "analysis", _COMMON_ANALYSIS | extras[kind], {"kind"})
    normalized = {"kind": kind}
    options = []
    for key, option in (("reltol", "reltol"), ("abstol_a", "abstol"), ("vntol_v", "vntol")):
        if key in raw:
            number = _number(raw[key], "analysis." + key, positive=True)
            if key == "reltol" and number > 1:
                raise SpiceError("analysis.reltol must be greater than zero and at most one")
            normalized[key] = number
            options.append(f"{option}={_literal(number)}")
    lines = [".options " + " ".join(options)] if options else []
    if kind == "op":
        lines.append(".op")
    elif kind == "tran":
        for field in ("step_s", "stop_s"):
            if field not in raw:
                raise SpiceError(f"analysis.{field} is required for transient analysis")
        step = _number(raw["step_s"], "analysis.step_s", positive=True)
        stop = _number(raw["stop_s"], "analysis.stop_s", positive=True)
        start = _number(raw.get("start_s", 0), "analysis.start_s")
        uic = raw.get("uic", False)
        if type(uic) is not bool or not 0 <= start < stop or step > stop - start:
            raise SpiceError("transient requires boolean uic, 0 <= start_s < stop_s, and step_s <= duration")
        if (stop - start) / step > 1_000_000:
            raise SpiceError("transient requests more than 1000000 nominal output intervals")
        normalized.update(step_s=step, stop_s=stop, start_s=start, uic=uic)
        line = f".tran {_literal(step)} {_literal(stop)} {_literal(start)}"
        if "max_step_s" in raw:
            maximum = _number(raw["max_step_s"], "analysis.max_step_s", positive=True)
            normalized["max_step_s"] = maximum
            line += " " + _literal(maximum)
        lines.append(line + (" uic" if uic else ""))
    elif kind == "ac":
        if not {"scale", "points", "start_hz", "stop_hz"} <= set(raw):
            raise SpiceError("AC analysis requires scale, points, start_hz, and stop_hz")
        scale, points = raw["scale"], raw["points"]
        if scale not in ("dec", "oct", "lin") or type(points) is not int or not 1 <= points <= 1_000_000:
            raise SpiceError("AC scale must be dec/oct/lin and points an integer from 1 to 1000000")
        start = _number(raw["start_hz"], "analysis.start_hz", positive=True)
        stop = _number(raw["stop_hz"], "analysis.stop_hz", positive=True)
        if stop <= start:
            raise SpiceError("AC stop_hz must be greater than start_hz")
        estimate = points if scale == "lin" else points * (math.log(stop) - math.log(start)) / math.log(10 if scale == "dec" else 2) + 1
        if estimate > 1_000_000:
            raise SpiceError("AC requests more than 1000000 frequency points")
        if not any(model["kind"] in ("V", "I") and model.get("ac_magnitude", 0) > 0 for model in models):
            raise SpiceError("AC analysis requires an explicit source model with positive ac_magnitude")
        normalized.update(scale=scale, points=points, start_hz=start, stop_hz=stop)
        lines.append(f".ac {scale} {points} {_literal(start)} {_literal(stop)}")
    else:
        if not {"source", "start", "stop", "step"} <= set(raw) or not isinstance(raw["source"], str):
            raise SpiceError("DC analysis requires source reference and numeric start, stop, and step")
        matches = [model for model in models if model["reference"] == raw["source"] and model["kind"] in ("V", "I")]
        if len(matches) != 1:
            raise SpiceError("DC source must identify an included explicit V or I component reference")
        start, stop, step = (_number(raw[field], "analysis." + field) for field in ("start", "stop", "step"))
        delta = stop - start
        if not math.isfinite(delta) or delta == 0 or step == 0 or delta / step <= 0 or not math.isfinite(delta / step) or abs(delta / step) > 1_000_000:
            raise SpiceError("DC sweep needs a nonzero correctly signed step and at most 1000000 intervals")
        normalized.update(source=raw["source"], start=start, stop=stop, step=step)
        lines.append(f'.dc {matches[0]["spice_reference"]} {_literal(start)} {_literal(stop)} {_literal(step)}')
    return normalized, lines


def generate_spice(design: Design | dict, analysis: dict, output: Path,
                   block_path: str | None = None, *, ground_nets: Sequence[str] = ("GND", "0")) -> dict:
    """Create an explicit nominal netlist and JSON manifest, never execute it.

    Model schema: ``parameters.spice={kind,value,pins,...}``, with kind R/C/L/V/I.
    Pins are an ordered pair of physical pin numbers, or positive/negative roles.
    Ground aliases are exact, case-sensitive global-net names supplied by caller.
    Both output and ``<output>.json`` must be new, non-symlink file paths.
    """
    try:
        parsed = Design.from_dict(design.to_dict() if isinstance(design, Design) else design)
    except (ModelError, TypeError, ValueError) as exc:
        raise SpiceError(f"Invalid canonical design: {exc}") from exc
    if isinstance(ground_nets, (str, bytes)) or not isinstance(ground_nets, (list, tuple)) or not ground_nets or any(not isinstance(net, str) or not net for net in ground_nets) or len(ground_nets) != len(set(ground_nets)):
        raise SpiceError("ground_nets must be a nonempty list/tuple of distinct exact global-net names")
    aliases = set(ground_nets)
    blocks = dict(parsed.walk_blocks())
    selection = parsed.root.id if block_path is None else block_path
    if not isinstance(selection, str) or selection not in blocks:
        raise SpiceError("block_path must identify an existing absolute block path")
    selected = sorted(((path, comp) for path, comp in walk_components(parsed)
                       if path.startswith(selection + ".")), key=lambda pair: pair[0])
    fitted = [(path, comp) for path, comp in selected if comp.fitted]
    if not fitted:
        raise SpiceError("selected block contains no fitted components")
    models = []
    for path, component in fitted:
        raw = component.parameters.get("spice")
        allowed = {"kind", "value", "pins", "tolerance", "ac_magnitude", "ac_phase_deg", "initial_voltage_v", "initial_current_a"}
        model = _object(raw, path + ".parameters.spice", allowed, {"kind", "value", "pins"})
        kind = model["kind"]
        if not isinstance(kind, str) or kind not in _KINDS:
            raise SpiceError(f"{path}: explicit model kind must be R, C, L, V, or I; no active-device model is inferred")
        pair = model["pins"]
        if isinstance(pair, dict):
            _object(pair, path + ".spice.pins", {"positive", "negative"}, {"positive", "negative"})
            pair = [pair["positive"], pair["negative"]]
        if not isinstance(pair, list) or len(pair) != 2 or any(not isinstance(pin, str) or not pin for pin in pair) or len(set(pair)) != 2 or set(pair) != set(component.pins):
            raise SpiceError(f"{path}: two distinct physical pins must resolve and account for every component pin")
        nets = [component.pins[pin] for pin in pair]
        if any(not net for net in nets):
            raise SpiceError(f"{path}: every simulated pin requires an explicit global net")
        reference = kind + "_" + re.sub(r"[^A-Za-z0-9_]", "_", component.reference)[:48] + "_" + hashlib.sha256(path.encode()).hexdigest()[:12]
        normalized = {"path": path, "reference": component.reference, "spice_reference": reference,
                      "kind": kind, "value_si": _value(model["value"], path + ".spice.value", positive=kind in {"R", "C", "L"}),
                      "pins": pair, "nets": nets}
        if "tolerance" in model:
            tolerance = _number(model["tolerance"], path + ".spice.tolerance")
            if kind not in {"R", "C", "L"} or not 0 <= tolerance <= 1:
                raise SpiceError(f"{path}: tolerance is a finite fractional value 0..1 for passive models")
            normalized["tolerance"] = tolerance
        for field in ("ac_magnitude", "ac_phase_deg"):
            if field in model:
                if kind not in {"V", "I"}:
                    raise SpiceError(f"{path}: {field} is only valid on V or I sources")
                number = _number(model[field], path + ".spice." + field)
                if field == "ac_magnitude" and number < 0:
                    raise SpiceError(f"{path}: ac_magnitude must be nonnegative")
                normalized[field] = number
        if "ac_phase_deg" in model and "ac_magnitude" not in model:
            raise SpiceError(f"{path}: ac_phase_deg requires an explicit ac_magnitude")
        for field, required_kind in (("initial_voltage_v", "C"), ("initial_current_a", "L")):
            if field in model:
                if kind != required_kind:
                    raise SpiceError(f"{path}: {field} is only valid on {required_kind}")
                normalized[field] = _number(model[field], path + ".spice." + field)
        models.append(normalized)
    if len({model["spice_reference"].casefold() for model in models}) != len(models):
        raise SpiceError("generated SPICE reference collision")
    nets = sorted({net for model in models for net in model["nets"]})
    if not aliases.intersection(nets):
        raise SpiceError("selected network has no explicitly configured ground net")
    net_map = {net: "0" if net in aliases else "n_" + hashlib.sha256(net.encode()).hexdigest()[:24] for net in nets}
    identifiers = [node for node in net_map.values() if node != "0"]
    if len(set(identifiers)) != len(identifiers):
        raise SpiceError("generated SPICE node collision")
    normalized_analysis, commands = _analysis(analysis, models)
    lines = ["PCBDev explicit nominal two-terminal simulation", f"* canonical design SHA256 {parsed.fingerprint()}",
             "* no hardware qualification; tolerance values are recorded but not swept"]
    for model in models:
        positive, negative = (net_map[net] for net in model["nets"])
        line = f'{model["spice_reference"]} {positive} {negative} '
        if model["kind"] in {"V", "I"}:
            line += "DC " + _literal(model["value_si"])
            if "ac_magnitude" in model:
                line += " AC " + _literal(model["ac_magnitude"]) + " " + _literal(model.get("ac_phase_deg", 0))
        else:
            line += _literal(model["value_si"])
            initial = model.get("initial_voltage_v", model.get("initial_current_a"))
            if initial is not None:
                line += " IC=" + _literal(initial)
        lines.append(line)
    text = "\n".join([*lines, *commands, ".end", ""])
    output_path = Path(output).absolute()
    manifest_path = output_path.with_name(output_path.name + ".json")
    if output_path.exists() or output_path.is_symlink() or manifest_path.exists() or manifest_path.is_symlink():
        raise SpiceError("SPICE output and manifest must be new, non-symlink paths")
    output_sha = hashlib.sha256(text.encode()).hexdigest()
    manifest = {"schema_version": 1, "generator": "pcb_dev", "kind": "explicit_spice_nominal",
                "design_sha256": parsed.fingerprint(), "output_sha256": output_sha,
                "block_path": selection, "analysis": normalized_analysis, "ground_nets": sorted(aliases),
                "net_map": net_map, "reference_map": {model["reference"]: model["spice_reference"] for model in models},
                "models": models, "excluded_dnp": [path for path, comp in selected if not comp.fitted],
                "executed": False, "hardware_validated": False, "nominal_only": True,
                "limitations": ["Explicit ideal R/C/L/V/I models only", "Tolerances recorded without corner or Monte Carlo simulation", "Block selection excludes external component models and does not invent boundary conditions", "No simulation engine was executed by generation"]}
    manifest_text = json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with output_path.open("x", encoding="utf-8") as stream:
            stream.write(text)
        with manifest_path.open("x", encoding="utf-8") as stream:
            stream.write(manifest_text)
    except OSError as exc:
        raise SpiceError(f"Could not create SPICE output: {exc}") from exc
    return {**manifest, "status": "generated", "path": str(output_path), "manifest_path": str(manifest_path)}
