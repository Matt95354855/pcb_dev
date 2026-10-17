"""Declared per-pin decoupling intent and independently measured PCB pads.

No capacitance, rating, distance limit, ground alias or datasheet requirement is
inferred. Passing these checks does not qualify a real capacitor, a return loop,
DC bias, ESR/ESL, native DRC or fabrication. Only declared rules are covered.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
import hashlib
import json
import math
from pathlib import Path
import re
import tempfile

from .model import Design, walk_components
from .reports import CheckReport, Issue


class DecouplingError(ValueError):
    """A rule or its layout evidence is missing, invalid or unsupported."""


MAX_RULES = 4096
MAX_BOARD_BYTES = 32 * 1024 * 1024
MAX_DISTANCE_COMPARISONS = 1_000_000
_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_FIELDS = {"id", "power_pin", "capacitor", "power_net", "return_net", "capacitance_min_f",
           "capacitance_max_f", "rail_max_voltage_v", "voltage_derating_factor", "max_distance_mm", "source"}


@dataclass(frozen=True)
class _Rule:
    owner: str
    data: dict
    target_path: str
    capacitor_path: str

    @property
    def path(self):
        return self.owner + "." + self.data["id"]


def _text(value, label):
    if type(value) is not str or not value.strip() or len(value) > 2048 or any(ord(char) < 32 for char in value):
        raise DecouplingError(f"{label} must be a bounded nonempty string without control characters")
    return value


def _object(value, fields, label):
    if type(value) is not dict or set(value) != fields:
        raise DecouplingError(f"{label} requires exactly {', '.join(sorted(fields))}")
    return value


def _number(value, label):
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise DecouplingError(f"{label} must be a finite positive number in the documented SI unit")
    return value


def _component_path(owner, value):
    value = _text(value, "component")
    if any(not _ID.fullmatch(part) for part in value.split(".")):
        raise DecouplingError("component must be a relative component path within the owning block, optionally through descendants")
    return owner + "." + value


def _rules(design, report):
    rules, count = [], 0
    for owner, block in design.walk_blocks():
        if "decoupling" not in block.resources:
            continue
        declared = block.resources["decoupling"]
        if type(declared) is not list:
            report.add(Issue("decoupling.invalid_rule", "error", "resources.decoupling must be an array", owner))
            continue
        ids = set()
        for raw in declared:
            count += 1
            if count > MAX_RULES:
                raise DecouplingError(f"At most {MAX_RULES} decoupling rules may be checked")
            try:
                data = _object(raw, _FIELDS, "decoupling rule")
                identifier = _text(data["id"], "id")
                if not _ID.fullmatch(identifier) or identifier in ids:
                    raise DecouplingError("Rule ids must be distinct identifiers within their owning block")
                ids.add(identifier)
                target = _object(data["power_pin"], {"component", "pin"}, "power_pin")
                capacitor = _object(data["capacitor"], {"component", "power_pin", "return_pin"}, "capacitor")
                for value in (target["pin"], capacitor["power_pin"], capacitor["return_pin"]):
                    _text(value, "pin number")
                if capacitor["power_pin"] == capacitor["return_pin"]:
                    raise DecouplingError("Capacitor power and return pins must be distinct")
                for key in ("power_net", "return_net"):
                    _text(data[key], key)
                if data["power_net"] == data["return_net"]:
                    raise DecouplingError("Power and return nets must be distinct exact global nets")
                for key in ("capacitance_min_f", "capacitance_max_f", "rail_max_voltage_v", "voltage_derating_factor", "max_distance_mm"):
                    _number(data[key], key)
                if data["capacitance_min_f"] > data["capacitance_max_f"] or data["voltage_derating_factor"] > 1:
                    raise DecouplingError("Capacitance bounds must be ordered and voltage_derating_factor must be <= 1")
                source = _object(data["source"], {"kind", "reference", "revision", "section"}, "source")
                if source["kind"] not in ("user_requirement", "datasheet"):
                    raise DecouplingError("source.kind must be user_requirement or datasheet; sources are declared, never authenticated here")
                for key in ("reference", "revision", "section"):
                    _text(source[key], "source." + key)
                rules.append(_Rule(owner, data, _component_path(owner, target["component"]),
                                   _component_path(owner, capacitor["component"])))
            except (ValueError, TypeError, KeyError) as exc:
                report.add(Issue("decoupling.invalid_rule", "error", str(exc), owner))
    report.metadata["rule_count"] = count
    return rules


def check_decoupling(design: Design) -> CheckReport:
    """Check only declared pin/capacitor/net/value/rating requirements (C6)."""
    report = CheckReport("decoupling-intent", metadata={
        "completed": False, "native": False, "scope": "declared-only", "source_verified": False,
        "placement_verified": False, "manufacturing_qualified": False, "validated_rules": 0,
        "sources": [], "declared_power_pins": [],
    })
    try:
        design = Design.from_dict(design.to_dict())
        report.metadata["design_hash"] = design.fingerprint()
        rules = _rules(design, report)
        components = dict(walk_components(design))
        used_caps, targets = {}, set()
        for rule in rules:
            data = rule.data
            before = len(report.issues)
            try:
                target, capacitor = components.get(rule.target_path), components.get(rule.capacitor_path)
                if target is None or capacitor is None:
                    raise DecouplingError("Target and capacitor must reference existing components within the owner's subtree")
                if rule.target_path == rule.capacitor_path:
                    raise DecouplingError("A component cannot decouple itself")
                if not target.fitted or not capacitor.fitted:
                    raise DecouplingError("Unfitted/DNP targets and capacitors cannot satisfy a decoupling rule")
                target_key = (rule.target_path, data["power_pin"]["pin"])
                declared_ports = [port for path, block in design.walk_blocks() for port in block.ports
                                  for binding in port.pin_bindings
                                  if (path + "." + binding.component, binding.pin) == target_key]
                if any(port.kind != "power" or port.net != data["power_net"] for port in declared_ports):
                    raise DecouplingError("Target contradicts an existing declared power-port pin/net mapping")
                if rule.capacitor_path in used_caps and used_caps[rule.capacitor_path] != target_key:
                    raise DecouplingError("A capacitor cannot satisfy independent per-pin rules for multiple power pins")
                used_caps[rule.capacitor_path] = target_key
                targets.add(target_key)
                pairs = ((target, data["power_pin"]["pin"], data["power_net"]),
                         (capacitor, data["capacitor"]["power_pin"], data["power_net"]),
                         (capacitor, data["capacitor"]["return_pin"], data["return_net"]))
                if any(component.pins.get(pin) != net for component, pin, net in pairs):
                    raise DecouplingError("Every target/capacitor pin must exist and match its exact declared power/return net")
                if set(capacitor.pins) != {data["capacitor"]["power_pin"], data["capacitor"]["return_pin"]}:
                    raise DecouplingError("This initial contract supports two-terminal capacitors only")
                capacitance = _number(capacitor.parameters.get("capacitance_f"), "capacitor.parameters.capacitance_f")
                rating = _number(capacitor.parameters.get("voltage_rating_v"), "capacitor.parameters.voltage_rating_v")
                if not data["capacitance_min_f"] <= capacitance <= data["capacitance_max_f"]:
                    report.add(Issue("decoupling.capacitance", "error", "Declared capacitance is outside the required range", rule.path,
                                     {"capacitance_f": capacitance, "minimum_f": data["capacitance_min_f"], "maximum_f": data["capacitance_max_f"]}))
                limit = rating * data["voltage_derating_factor"]
                if data["rail_max_voltage_v"] > limit:
                    report.add(Issue("decoupling.voltage_rating", "error", "Declared maximum rail voltage exceeds the derated capacitor rating", rule.path,
                                     {"rail_max_voltage_v": data["rail_max_voltage_v"], "voltage_rating_v": rating, "factor": data["voltage_derating_factor"], "derated_limit_v": limit}))
                # Reject known contradictions instead of trusting a lower
                # per-rule voltage to erase already-declared rail conditions.
                declared = [abs(value) for _, block in design.walk_blocks() for port in block.ports
                            if port.kind == "power" and port.net == data["power_net"]
                            for value in (port.voltage_min, port.voltage_max) if value is not None]
                if declared and max(declared) > data["rail_max_voltage_v"]:
                    report.add(Issue("decoupling.rail_limit", "error", "Rule voltage limit is below an already-declared power-net voltage magnitude", rule.path,
                                     {"known_voltage_v": max(declared), "rail_max_voltage_v": data["rail_max_voltage_v"]}))
                report.metadata["sources"].append({"rule": rule.path, **data["source"], "verified": False})
            except (ValueError, TypeError, KeyError) as exc:
                report.add(Issue("decoupling.intent", "error", str(exc), rule.path))
            if len(report.issues) == before:
                report.metadata["validated_rules"] += 1
        report.metadata["declared_power_pins"] = [{"component": path, "pin": pin} for path, pin in sorted(targets)]
        report.metadata["coverage"] = "declared-rules-only" if report.metadata["rule_count"] else "not-declared"
        report.metadata["completed"] = True
    except Exception as exc:
        report.add(Issue("decoupling.incomplete", "blocker", f"Decoupling intent could not complete: {type(exc).__name__}: {exc}"))
    return report


def _children(node, tag):
    return [item for item in node[1:] if isinstance(item, list) and item and item[0] == tag]


def _one(node, tag, *, required=True):
    found = _children(node, tag)
    if len(found) != 1 and (required or found):
        raise DecouplingError(f"Expected one unambiguous PCB {tag} field")
    return found[0] if found else None


def _float(value):
    if not isinstance(value, str):
        raise DecouplingError("PCB coordinates must be numeric atoms")
    result = float(value)
    if not math.isfinite(result):
        raise DecouplingError("PCB coordinates must be finite")
    return result


def _pose(footprint):
    node = _one(footprint, "at")
    if len(node) not in (3, 4):
        raise DecouplingError("PCB footprint pose requires x, y and optional angle")
    return _float(node[1]), _float(node[2]), _float(node[3]) if len(node) == 4 else 0.0


def _pad_signature(node):
    """Geometry only, with 1e-6 mm/degree rounding for native serialization."""
    from .libraries import _pad
    pad = _pad(node)
    if pad.shape not in {"rect", "circle", "oval", "roundrect", "trapezoid"}:
        raise DecouplingError("Unsupported pad shape for pinned geometric evidence")
    geometry = asdict(pad)
    geometry["rotation"] = round(geometry["rotation"] % 360, 6) % 360
    geometry["layers"] = sorted(geometry["layers"])
    def normalized(value):
        if isinstance(value, list):
            return [normalized(item) for item in value]
        if isinstance(value, dict):
            return {key: normalized(item) for key, item in value.items()}
        if isinstance(value, float):
            return round(value, 6)
        return value
    extras = []
    known = {"at", "size", "layers", "drill", "roundrect_rratio", "chamfer_ratio", "chamfer", "rect_delta",
             "net", "uuid", "tstamp", "pinfunction", "pintype", "solder_mask_margin", "solder_paste_margin",
             "solder_paste_margin_ratio", "clearance", "zone_connect", "thermal_bridge_angle", "thermal_gap",
             "thermal_bridge_width", "remove_unused_layers", "keep_end_layers", "property", "teardrops"}
    for item in node[4:]:
        if not isinstance(item, list) or not item or item[0] not in known:
            raise DecouplingError("Unsupported pad geometry field; proximity is unverified")
        if item[0] in {"roundrect_rratio", "chamfer_ratio", "chamfer", "rect_delta"}:
            if item[0] == "chamfer":
                values = item[1:]
                if not values or set(values) - {"top_left", "top_right", "bottom_left", "bottom_right"}:
                    raise DecouplingError("Unsupported pad chamfer corners")
            else:
                values = [round(_float(atom), 6) for atom in item[1:]]
                if item[0] != "rect_delta" and (len(values) != 1 or not 0 <= values[0] <= 0.5):
                    raise DecouplingError("Invalid rounded/chamfered pad ratio")
                if item[0] == "rect_delta" and len(values) != 2:
                    raise DecouplingError("Pad rectangle delta requires x/y")
            extras.append([item[0], *values])
    return json.dumps([normalized(geometry), sorted(extras)], sort_keys=True, allow_nan=False), pad


def _board_evidence(content, bound, expected):
    from .parity import parse_sexpr
    root = parse_sexpr(content.decode("utf-8-sig"), preserve_quotes=True)
    # Library inventory parsing uses plain string atoms, not quotation classes.
    def plain(value):
        return [plain(item) for item in value] if isinstance(value, list) else str(value)
    root = plain(root)
    entries = json.loads(bound.bindings_content)["components"]
    result = {}
    for footprint in _children(root, "footprint"):
        reference = next((item[2] for item in _children(footprint, "property") if len(item) >= 3 and item[1] == "Reference"), None)
        if reference is None:
            reference = next((item[2] for item in _children(footprint, "fp_text") if len(item) >= 3 and item[1] == "reference"), None)
        if reference not in expected or reference in result:
            raise DecouplingError("PCB footprint identity is missing, duplicated or outside the bound design")
        layer = _one(footprint, "layer")
        if len(layer) != 2 or layer[1] != "F.Cu":
            raise DecouplingError("Only explicitly placed front-side footprints are supported")
        pose = _pose(footprint)
        path = expected[reference]["path"]
        binding = bound.components[path]
        if binding.component.position is not None and binding.component.position.side != "top":
            raise DecouplingError("Only top-side model placement can participate in this layout proof")
        alias = entries[path]["footprint_library"]
        source = plain(parse_sexpr(bound.source_contents[alias].decode("utf-8-sig"), preserve_quotes=True))
        actual_pads = [_pad_signature(node) for node in _children(footprint, "pad")]
        required_pads = [_pad_signature(node) for node in _children(source, "pad")]
        if Counter(signature for signature, _ in actual_pads) != Counter(signature for signature, _ in required_pads):
            raise DecouplingError(f"{reference}: actual pad geometry differs from captured pinned footprint source")
        dnp = any("dnp" in item[1:] for item in _children(footprint, "attr"))
        pads = {}
        angle = math.radians(pose[2])
        for _, pad in actual_pads:
            if pad.type == "np_thru_hole" or not pad.number:
                continue
            point = (pose[0] + pad.x_mm * math.cos(angle) + pad.y_mm * math.sin(angle),
                     pose[1] - pad.x_mm * math.sin(angle) + pad.y_mm * math.cos(angle))
            if not all(math.isfinite(value) for value in point):
                raise DecouplingError("PCB pad transformation produced a nonfinite coordinate")
            pads.setdefault(pad.number, []).append(point)
        result[path] = {"pads": pads, "dnp": dnp, "pose": pose, "position_explicit": binding.component.position is not None}
    return result


def check_layout(design: Design, board: str | Path, bindings=None) -> CheckReport:
    """Measure declared proximity using actual, pinned, top-side PCB pad centres.

    Metric: every physical pad of the target pin must be within max_distance_mm
    of a capacitor power-pin pad (max of nearest distances). Return routing and
    copper path lengths are outside this metric. Missing evidence blocks it.
    """
    intent = check_decoupling(design)
    report = CheckReport("decoupling-layout", metadata={
        "completed": False, "native": False, "scope": "declared-only", "design_hash": intent.metadata.get("design_hash"),
        "source_verified": False, "distance_verified": False, "manufacturing_qualified": False,
        "metric": "max-target-pad-to-nearest-capacitor-power-pad-centre-mm", "geometry_tolerance_mm": 1e-6,
        "measurements": [], "rule_count": intent.metadata.get("rule_count", 0),
        "routing_verified": False, "return_loop_verified": False,
    })
    try:
        if intent.blocking:
            raise DecouplingError("Decoupling intent has unresolved errors; layout evidence cannot satisfy it")
        design = Design.from_dict(design.to_dict())
        rules = _rules(design, report)
        if not rules:
            report.metadata.update(completed=True, coverage="not-declared")
            return report
        from .bindings import BoundDesign, load_bindings
        from .bound_parity import _board_details, _expected
        if bindings is None:
            raise DecouplingError("Pinned library bindings are required; generic pads or component centres cannot prove proximity")
        bound = bindings if isinstance(bindings, BoundDesign) else load_bindings(design, bindings)
        if bound.design.fingerprint() != design.fingerprint():
            raise DecouplingError("Bindings do not describe the current canonical design")
        expected = _expected(bound)
        path = Path(board)
        if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= MAX_BOARD_BYTES:
            raise DecouplingError("PCB input must be a bounded regular non-symlink file")
        content = path.read_bytes()
        if not 0 < len(content) <= MAX_BOARD_BYTES:
            raise DecouplingError("PCB input exceeds its byte bound")
        report.metadata.update(board_sha256=hashlib.sha256(content).hexdigest(),
                               bindings_sha256=bound.provenance["bindings_sha256"],
                               registry_sha256=bound.provenance["registry_sha256"], library_sources=bound.provenance["sources"])
        # Net/name/type and explicit-NC parity operates on the captured bytes,
        # rather than rereading a potentially changing board between checks.
        with tempfile.TemporaryDirectory(prefix="pcb-decoupling-") as temporary:
            captured = Path(temporary) / "board.kicad_pcb"
            captured.write_bytes(content)
            _board_details(captured, expected)
        actual = _board_evidence(content, bound, expected)
        comparisons = 0
        for rule in rules:
            target, capacitor = actual[rule.target_path], actual[rule.capacitor_path]
            if target["dnp"] or capacitor["dnp"] or not target["position_explicit"] or not capacitor["position_explicit"]:
                raise DecouplingError("DNP components and missing explicit model placements cannot satisfy layout rules")
            target_pads = target["pads"].get(rule.data["power_pin"]["pin"], [])
            capacitor_pads = capacitor["pads"].get(rule.data["capacitor"]["power_pin"], [])
            if not target_pads or not capacitor_pads:
                raise DecouplingError("A target or capacitor pin has no actual electrical pad")
            comparisons += len(target_pads) * len(capacitor_pads)
            if comparisons > MAX_DISTANCE_COMPARISONS:
                raise DecouplingError("Declared pad proximity exceeds its bounded comparison budget")
            nearest = [(min((math.dist(point, other), other) for other in capacitor_pads), point) for point in target_pads]
            (distance, cap_point), target_point = max(nearest)
            if not math.isfinite(distance):
                raise DecouplingError("PCB pad distance exceeds finite numeric range")
            record = {"rule": rule.path, "target_component": rule.target_path, "capacitor_component": rule.capacitor_path,
                      "distance_mm": distance, "max_distance_mm": rule.data["max_distance_mm"],
                      "target_pad_centres_mm": target_pads, "capacitor_power_pad_centres_mm": capacitor_pads,
                      "limiting_target_centre_mm": target_point, "nearest_capacitor_centre_mm": cap_point,
                      "passed": distance <= rule.data["max_distance_mm"] + 1e-9}
            report.metadata["measurements"].append(record)
            if not record["passed"]:
                report.add(Issue("decoupling.distance", "error", "Actual power-pin pad proximity exceeds the declared limit", rule.path, record))
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != report.metadata["board_sha256"]:
            raise DecouplingError("PCB changed while decoupling evidence was being measured")
        report.metadata.update(completed=True, distance_verified=True, coverage="declared-rules-only")
    except Exception as exc:
        report.add(Issue("decoupling.layout_unverified", "blocker", f"Pad proximity could not be verified: {type(exc).__name__}: {exc}"))
    return report
