"""Conservative, metadata-based electrical preflight.

These checks reason about declared block interfaces and component ratings. They
do not inspect a native schematic, infer library pin correctness, simulate a
circuit, or replace KiCad ERC/DRC and engineering review.
"""

from __future__ import annotations

from collections import defaultdict
import math
from typing import Any, Iterable

from .reports import CheckReport, Issue, Severity


class _Nets:
    def __init__(self, names: Iterable[str]):
        self.parent = {name: name for name in names}

    def find(self, name: str) -> str:
        root = name
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[name] != name:
            previous = self.parent[name]
            self.parent[name] = root
            name = previous
        return root

    def join(self, left: str, right: str) -> None:
        left, right = self.find(left), self.find(right)
        if left != right:
            self.parent[max(left, right)] = min(left, right)


def _number(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def _normal(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())


def _attribute_value(attribute: str, value: str) -> str:
    # '+' and '-' have electrical meaning and must not disappear during the
    # punctuation normalization used for protocol/logic-standard names.
    if attribute == "polarity":
        symbolic = {"+": "positive", "+1": "positive", "-": "negative", "-1": "negative"}
        if value.strip() in symbolic:
            return symbolic[value.strip()]
    return _normal(value)


def _issue(report: CheckReport, code: str, message: str, path: str = "", *,
           severity: Severity = Severity.ERROR, **details: Any) -> None:
    report.add(Issue(code, severity, message, path, details))


def _cycles(edges: dict[str, list[str]]) -> list[list[str]]:
    """Return DFS back-edge cycles, without recursing through an invalid graph."""
    state: dict[str, int] = {}
    result: list[list[str]] = []
    # Iterative DFS also handles deeply nested dependency graphs safely.
    for start in sorted(edges):
        if state.get(start):
            continue
        stack: list[tuple[str, Any]] = [(start, iter(sorted(edges[start])))]
        path = [start]
        state[start] = 1
        while stack:
            node, targets = stack[-1]
            target = next(targets, None)
            if target is None:
                state[node] = 2
                stack.pop()
                path.pop()
            elif target not in edges:
                continue
            elif state.get(target) == 1:
                result.append(path[path.index(target):] + [target])
            elif not state.get(target):
                state[target] = 1
                path.append(target)
                stack.append((target, iter(sorted(edges[target]))))
    return result


def _validate_declared_port(path: str, port: Any, report: CheckReport) -> None:
    for key in ("voltage_min", "voltage_max", "max_current_ma", "current_draw_ma", "impedance_ohm"):
        value = getattr(port, key)
        if value is not None and not _number(value):
            _issue(report, "port.invalid_numeric", f"Port {key} must be a finite number.", path, field=key)
    low, high = port.voltage_min, port.voltage_max
    if _number(low) and _number(high) and low > high:
        _issue(report, "voltage.invalid_range", "Minimum voltage exceeds maximum voltage.", path,
               voltage_min=low, voltage_max=high)
    for key in ("max_current_ma", "current_draw_ma"):
        value = getattr(port, key)
        if _number(value) and value < 0:
            _issue(report, "power.invalid_current", f"{key} cannot be negative.", path, field=key, value=value)
    if _number(port.impedance_ohm) and port.impedance_ohm <= 0:
        _issue(report, "signal.invalid_impedance", "Declared impedance must be positive.", path)


def _check_net(entries: list[tuple[str, Any]], names: list[str], report: CheckReport) -> None:
    entries = sorted(entries)
    location = entries[0][0]
    net_label = ", ".join(names) if names else location
    details = {"ports": [path for path, _ in entries], "nets": names}
    kinds = {port.kind for _, port in entries}
    if len(kinds) > 1:
        _issue(report, "connection.kind_mismatch", f"Net {net_label} joins incompatible interface kinds.", location,
               kinds=sorted(kinds), **details)
    outputs = [(path, port) for path, port in entries if port.direction == "output" and port.kind != "ground"]
    if len(outputs) > 1:
        _issue(report, "connection.output_conflict", f"Net {net_label} joins multiple output drivers.", location,
               outputs=[path for path, _ in outputs], **details)

    # Protocol, polarity and logic standard are properties of the shared net,
    # so compare every declared value, including through passive relay ports.
    for attribute, code in (("protocol", "connection.protocol_mismatch"),
                            ("logic_standard", "signal.logic_mismatch"),
                            ("polarity", "connection.polarity_mismatch")):
        values = {getattr(port, attribute) for _, port in entries if getattr(port, attribute) is not None}
        if len({_attribute_value(attribute, value) for value in values}) > 1:
            _issue(report, code, f"Net {net_label} has incompatible declared {attribute} values.", location,
                   values=sorted(values), **details)

    drivers = [(path, port) for path, port in entries
               if port.direction in ("output", "bidirectional") and port.kind != "ground"]
    sinks = [(path, port) for path, port in entries if port.direction != "output"]
    for source_path, source in drivers:
        for target_path, target in sinks:
            if source_path == target_path:
                continue
            comparisons = []
            if _number(source.voltage_min) and _number(target.voltage_min):
                comparisons.append(source.voltage_min < target.voltage_min)
            if _number(source.voltage_max) and _number(target.voltage_max):
                comparisons.append(source.voltage_max > target.voltage_max)
            if any(comparisons):
                _issue(report, "voltage.incompatible", f"Output voltage of {source_path} exceeds the acceptable range of {target_path}.", target_path,
                       source=source_path, source_range=[source.voltage_min, source.voltage_max],
                       target_range=[target.voltage_min, target.voltage_max], nets=names)

    if "differential" in kinds:
        impedances = [(path, port.impedance_ohm) for path, port in entries if _number(port.impedance_ohm)]
        if impedances and any(not math.isclose(value, impedances[0][1], rel_tol=0.05)
                              for _, value in impedances[1:]):
            _issue(report, "signal.impedance_mismatch", "Differential interface impedances differ by more than five percent.", location,
                   impedances={path: value for path, value in impedances}, **details)

    if "power" not in kinds:
        return
    sources = [(path, port) for path, port in entries if port.kind == "power" and port.direction == "output"]
    loads = [(path, port) for path, port in entries if port.kind == "power" and port.direction in ("input", "bidirectional")]
    total = math.fsum(port.current_draw_ma for _, port in loads if _number(port.current_draw_ma))
    budgets = report.metadata.setdefault("power_budgets", [])
    budgets.append({**details, "sources": [path for path, _ in sources], "loads": [path for path, _ in loads],
                    "total_current_ma": total})
    if loads and not sources:
        _issue(report, "power.no_source", f"Power net {net_label} has no declared output source.", location, **details)
    # Each physical rail load is counted once, regardless of how many connection
    # edges or aliases reach it. Multiple sources are a separate driver conflict;
    # their capacities are never silently added as if parallel sharing were safe.
    for source_path, source in sources:
        if source.voltage_min is None or source.voltage_max is None:
            _issue(report, "power.voltage_undeclared", "Power-source voltage range is required to establish interface compatibility.", source_path)
        if source.max_current_ma is None:
            _issue(report, "power.capacity_undeclared", "Power-source current capacity is required for a rail budget.", source_path,
                   total_current_ma=total, nets=names)
        elif _number(source.max_current_ma) and total > source.max_current_ma + 1e-9:
            _issue(report, "power.budget_exceeded", f"Rail demand {total:g} mA exceeds the source capacity {source.max_current_ma:g} mA.", source_path,
                   total_current_ma=total, max_current_ma=source.max_current_ma, nets=names)
    for path, load in loads:
        if load.voltage_min is None or load.voltage_max is None:
            _issue(report, "power.load_voltage_undeclared", "Power-input voltage tolerance is required to establish compatibility.", path)


def _check_resources(blocks: dict[str, Any], nets: _Nets, report: CheckReport) -> None:
    addresses: dict[tuple[str, int], list[str]] = defaultdict(list)
    sequence: dict[str, list[str]] = {path: [] for path in blocks}
    dependencies = {path: list(block.depends_on) for path, block in blocks.items()}
    for path, block in blocks.items():
        for dependency in dependencies[path]:
            if dependency not in blocks:
                _issue(report, "dependency.missing", "Block dependency does not exist.", path, dependency=dependency)
        reservations = block.resources.get("i2c", [])
        if isinstance(reservations, dict):
            reservations = [reservations]
        if not isinstance(reservations, list):
            _issue(report, "resource.invalid_i2c", "resources.i2c must be a list of bus/address reservations.", path)
            reservations = []
        for reservation in reservations:
            if not isinstance(reservation, dict):
                _issue(report, "resource.invalid_i2c", "An I2C reservation must contain bus and address.", path)
                continue
            bus, address = reservation.get("bus"), reservation.get("address")
            try:
                if isinstance(address, str):
                    address = int(address, 0)
            except ValueError:
                pass
            if not isinstance(bus, str) or not bus.strip() or type(address) is not int or not 0x08 <= address <= 0x77:
                _issue(report, "resource.invalid_i2c", "I2C reservations require a bus and an unreserved 7-bit address (0x08–0x77).", path,
                       reservation=reservation)
                continue
            bus = nets.find(bus) if bus in nets.parent else bus
            addresses[(bus, address)].append(path)
        declared_sequence = block.resources.get("power_sequence", [])
        delay, required_delay = None, None
        if isinstance(declared_sequence, dict):
            delay = declared_sequence.get("delay_ms")
            required_delay = declared_sequence.get("required_delay_ms")
            declared_sequence = declared_sequence.get("after", [])
        if not isinstance(declared_sequence, list) or not all(isinstance(item, str) for item in declared_sequence):
            _issue(report, "power.invalid_sequence", "Power sequencing must declare a list of predecessor block paths.", path)
            continue
        sequence[path] = list(declared_sequence)
        for predecessor in declared_sequence:
            if predecessor not in blocks:
                _issue(report, "power.sequence_missing", "Power-sequence predecessor does not exist.", path, predecessor=predecessor)
        if delay is not None or required_delay is not None:
            if not _number(delay) or not _number(required_delay) or delay < 0 or required_delay < 0:
                _issue(report, "power.invalid_sequence_delay", "Sequencing delays must be finite nonnegative delay_ms and required_delay_ms values.", path)
            elif delay < required_delay:
                _issue(report, "power.sequence_delay", "Declared sequencing delay is shorter than the required delay.", path,
                       delay_ms=delay, required_delay_ms=required_delay)
    for (bus, address), owners in sorted(addresses.items()):
        if len(owners) > 1:
            _issue(report, "resource.i2c_collision", f"I2C address 0x{address:02x} is reserved more than once on bus {bus}.", owners[0],
                   bus=bus, address=address, owners=owners)
    for cycle in _cycles(dependencies):
        _issue(report, "dependency.cycle", "Block dependencies contain a cycle.", cycle[0], cycle=cycle)
    for cycle in _cycles(sequence):
        _issue(report, "power.sequence_cycle", "Power-sequence declarations contain a cycle.", cycle[0], cycle=cycle)


def _check_ratings(blocks: dict[str, Any], report: CheckReport) -> None:
    pairs = (("voltage_rating_v", "applied_voltage_v", "voltage"),
             ("current_rating_ma", "applied_current_ma", "current"),
             ("power_rating_w", "power_dissipation_w", "power"))
    count = 0
    for block_path, block in blocks.items():
        for component in block.components:
            if not component.fitted:
                continue
            path = f"{block_path}.{component.id}"
            parameters = component.parameters
            factor = parameters.get("derating_factor", 1.0)
            if not _number(factor) or not 0 < factor <= 1:
                _issue(report, "component.invalid_derating", "Derating factor must be greater than zero and at most one.", path)
                continue
            for rating_key, applied_key, quantity in pairs:
                if rating_key not in parameters and applied_key not in parameters:
                    continue
                rating, applied = parameters.get(rating_key), parameters.get(applied_key)
                if rating is None or applied is None:
                    _issue(report, "component.incomplete_rating", f"Both {rating_key} and {applied_key} are required to verify a component rating.", path,
                           severity=Severity.WARNING, quantity=quantity)
                    continue
                count += 1
                if not _number(rating) or not _number(applied) or rating <= 0 or applied < 0:
                    _issue(report, "component.invalid_rating", f"Invalid declared {quantity} rating or operating value.", path, quantity=quantity)
                elif applied > rating * factor + 1e-12:
                    _issue(report, "component.rating_exceeded", f"Declared operating {quantity} exceeds the derated component rating.", path,
                           quantity=quantity, rating=rating, applied=applied, derating_factor=factor)
            if any(key in parameters for key in ("temperature_min_c", "temperature_max_c", "operating_temperature_c")):
                low, high, actual = (parameters.get(key) for key in ("temperature_min_c", "temperature_max_c", "operating_temperature_c"))
                if not all(_number(value) for value in (low, high, actual)) or low > high:
                    _issue(report, "component.invalid_temperature", "Component temperature checks require a valid minimum, maximum and operating temperature.", path)
                elif not low <= actual <= high:
                    _issue(report, "component.temperature_exceeded", "Operating temperature is outside the declared component rating.", path,
                           minimum=low, maximum=high, operating=actual)
    report.metadata["component_rating_checks"] = count


def check_design(design: Any, *, require_physical_ports: bool = False) -> CheckReport:
    """Check declared interfaces, rail budgets, resources and frozen blocks.

    The model parser is the primary structural boundary; these checks also
    reject unsafe direct mutations of common electrical metadata. A report
    records its exact design fingerprint for later release gates and waivers.
    """
    from .model import freeze_fingerprint, walk_blocks
    from .expressions import resolve_parameters, evaluate_constraint
    from .physical_ports import check_physical_ports
    from .decoupling import check_decoupling

    if type(require_physical_ports) is not bool:
        raise ValueError("require_physical_ports must be a boolean")

    report = CheckReport("preflight", metadata={"completed": False, "native": False,
                                               "scope": "declared-interface-metadata"})
    try:
        report.metadata["design_hash"] = design.fingerprint()
        physical = check_physical_ports(design, require_physical_ports=require_physical_ports)
        report.issues.extend(physical.issues)
        report.metadata["physical_ports"] = physical.metadata["coverage"]
        decoupling = check_decoupling(design)
        report.issues.extend(decoupling.issues)
        report.metadata["decoupling"] = decoupling.metadata
        blocks = dict(walk_blocks(design.root))
        ports = {f"{path}.{port.id}": port for path, block in blocks.items() for port in block.ports}
        nets = _Nets(ports)
        connected: set[str] = set()
        named_nets: dict[str, str] = {}
        for connection in design.connections:
            missing = [path for path in (connection.source, connection.target) if path not in ports]
            if missing:
                _issue(report, "connection.missing_endpoint", "Connection refers to an unknown port.", connection.source,
                       missing=missing, net=connection.net)
                continue
            connected.update((connection.source, connection.target))
            nets.join(connection.source, connection.target)
            if connection.net in named_nets:
                nets.join(connection.source, named_nets[connection.net])
            else:
                named_nets[connection.net] = connection.source
            if connection.source == connection.target:
                _issue(report, "connection.self_connection", "A port cannot be connected only to itself.", connection.source)
        # Explicit Port.net is a physical global net, even if no logical edge
        # joins two interfaces. It must participate in driver and rail checks.
        physical_names: dict[str, str] = {}
        for path, port in ports.items():
            if type(port.net) is str and port.net:
                if port.net in named_nets:
                    nets.join(path, named_nets[port.net])
                if port.net in physical_names:
                    nets.join(path, physical_names[port.net])
                else:
                    physical_names[port.net] = path
        groups: dict[str, list[tuple[str, Any]]] = defaultdict(list)
        labels: dict[str, set[str]] = defaultdict(set)
        for name, path in named_nets.items():
            labels[nets.find(path)].add(name)
        for name, path in physical_names.items():
            labels[nets.find(path)].add(name)
        for path, port in sorted(ports.items()):
            _validate_declared_port(path, port, report)
            if port.required and path not in connected:
                _issue(report, "connection.required_unconnected", "Required block port is not connected.", path)
            if path in connected or type(port.net) is str and port.net:
                groups[nets.find(path)].append((path, port))
        for root, entries in sorted(groups.items()):
            _check_net(entries, sorted(labels[root]), report)
            kinds = {port.kind for _, port in entries}
            if kinds - {"power", "ground"} and all(port.direction in ("input", "passive") for _, port in entries):
                _issue(report, "connection.no_driver", "Signal net has no output or bidirectional driver.", entries[0][0],
                       ports=[path for path, _ in entries])
        _check_resources(blocks, nets, report)
        _check_ratings(blocks, report)
        report.metadata["manufacturer_rules_verified"] = False
        _issue(report, "manufacturer.rules_unverified",
               "No revisioned manufacturer rule set was checked; board dimensions and layers are planning data only. Confirm current supplier capabilities before ordering.",
               design.root.id, severity=Severity.WARNING, manufacturer=design.board.manufacturer)
        resolved = None
        try:
            resolved = resolve_parameters(design)
        except (ValueError, TypeError, KeyError, ArithmeticError) as exc:
            _issue(report, "expression.invalid", f"Parameter equations cannot be resolved: {exc}", design.root.id)
        if resolved is not None:
            report.metadata["resolved_parameters"] = resolved
            for path, block in blocks.items():
                for constraint in block.constraints:
                    expression = constraint.expression if hasattr(constraint, "expression") else constraint["expression"]
                    message = constraint.message if hasattr(constraint, "message") else constraint["message"]
                    try:
                        valid = evaluate_constraint(expression, resolved[path])
                    except (ValueError, TypeError, KeyError, ArithmeticError) as exc:
                        _issue(report, "constraint.invalid", f"Constraint cannot be evaluated: {exc}", path, expression=expression)
                    else:
                        if valid is not True:
                            _issue(report, "constraint.failed", message, path, expression=expression)
        for path, block in blocks.items():
            if not block.frozen:
                continue
            if not block.frozen_hash:
                _issue(report, "frozen.unrecorded", "Frozen block has no recorded dependency fingerprint.", path)
            elif resolved is None:
                _issue(report, "frozen.unverifiable", "Frozen block cannot be verified because parameter resolution failed.", path)
            else:
                expected = freeze_fingerprint(design, path, resolved)
                if expected != block.frozen_hash:
                    _issue(report, "frozen.invalidated", "Frozen block or its inherited parameters/dependencies changed; revalidation is required.", path,
                           recorded_hash=block.frozen_hash, current_hash=expected)
        report.metadata["block_count"] = len(blocks)
        report.metadata["port_count"] = len(ports)
        report.metadata["net_count"] = len(groups)
        report.metadata["completed"] = (physical.metadata.get("completed") is True
                                        and decoupling.metadata.get("completed") is True)
    except Exception as exc:
        # Never report a clean preflight after an unexpected checker/model error.
        _issue(report, "preflight.incomplete", f"Preflight could not complete: {type(exc).__name__}: {exc}",
               severity=Severity.BLOCKER)
    return report
