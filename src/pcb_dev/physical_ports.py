"""Check explicit interface intentions against declared component pin nets.

An interface target is a direct component of the port's own block. This module
does not infer a datasheet role from a pin name, propagate through a component,
rewrite copper/net names, or certify a footprint. Abstract legacy interfaces
remain valid drafts, with explicit incomplete coverage unless strict mode is
requested.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from .model import Design
from .reports import CheckReport, Issue, Severity


class _Groups:
    def __init__(self, paths: dict[str, Any]):
        self.parents = {path: path for path in paths}

    def find(self, path: str) -> str:
        while path != self.parents[path]:
            self.parents[path] = self.parents[self.parents[path]]
            path = self.parents[path]
        return path

    def join(self, left: str, right: str) -> None:
        left, right = self.find(left), self.find(right)
        if left != right:
            self.parents[max(left, right)] = min(left, right)


def check_physical_ports(design: Design, *, require_physical_ports: bool = False) -> CheckReport:
    """Report physical pin/net coverage separately from abstract connections.

    Exact net contradictions always block. Missing/DNP realization warns for a
    draft and blocks strict checks. Connection edges remain independently
    required by the existing ``Port.required`` contract.
    """
    if type(require_physical_ports) is not bool:
        raise ValueError("require_physical_ports must be a boolean")
    report = CheckReport("physical-ports", metadata={
        "completed": False, "native": False, "scope": "declared-component-pin-net-consistency",
    })
    try:
        report.metadata["design_hash"] = design.fingerprint()
        blocks = dict(design.walk_blocks())
        ports = {f"{block_path}.{port.id}": port for block_path, block in blocks.items() for port in block.ports}
        groups = _Groups(ports)
        records: dict[str, dict[str, Any]] = {}
        owners: dict[tuple[str, str], str] = {}
        mapped_pins: set[tuple[str, str]] = set()

        def finding(code: str, message: str, path: str, details: dict[str, Any] | None = None,
                    severity: Severity = Severity.ERROR) -> None:
            report.add(Issue(code, severity, message, path, details or {}))

        for block_path, block in blocks.items():
            components = {component.id: component for component in block.components}
            for port in block.ports:
                path = f"{block_path}.{port.id}"
                record = {"path": path, "net": port.net, "status": "abstract", "realized": False, "targets": []}
                records[path] = record
                if port.net is not None and (type(port.net) is not str or not port.net.strip()):
                    record["status"] = "invalid"
                    finding("physical_ports.invalid_net", "Physical port net must be a nonempty string.", path)
                    continue
                if type(port.pin_bindings) is not list:
                    record["status"] = "invalid"
                    finding("physical_ports.invalid_target", "Physical pin targets must be an array.", path)
                    continue
                if port.net is not None:
                    record["status"] = "net_only"
                if not port.pin_bindings:
                    continue
                if port.net is None:
                    record["status"] = "invalid"
                    finding("physical_ports.missing_net", "Physical pin targets require an explicit port net.", path)
                valid = port.net is not None
                mounted = True
                for binding in port.pin_bindings:
                    component_id = getattr(binding, "component", None)
                    pin = getattr(binding, "pin", None)
                    if type(component_id) is not str or type(pin) is not str or component_id not in components:
                        valid = False
                        finding("physical_ports.invalid_target", "Target must identify a direct component of the port's own block and a pin number.", path)
                        continue
                    component = components[component_id]
                    component_path = f"{block_path}.{component_id}"
                    if pin not in component.pins:
                        valid = False
                        finding("physical_ports.missing_pin", "Target pin has no declared component net; an explicit no-connect cannot realize a port.", path,
                                {"component": component_path, "pin": pin})
                        continue
                    physical_net = component.pins[pin]
                    target = {"component": component_id, "component_path": component_path,
                              "pin": pin, "net": physical_net, "fitted": component.fitted}
                    record["targets"].append(target)
                    identity = (component_path, pin)
                    mapped_pins.add(identity)
                    if identity in owners:
                        valid = False
                        prior = owners[identity]
                        if prior in records:
                            records[prior].update(status="invalid", realized=False)
                        finding("physical_ports.pin_reused", "A physical pin cannot realize more than one port target.", path,
                                {"component": component_path, "pin": pin, "other_port": prior})
                    else:
                        owners[identity] = path
                    if physical_net != port.net:
                        valid = False
                        finding("physical_ports.pin_net_mismatch", "Target pin net does not match the explicit port net.", path,
                                {"component": component_path, "pin": pin, "port_net": port.net, "pin_net": physical_net})
                    if component.fitted is not True:
                        mounted = False
                # A prior duplicate may have invalidated this same record.
                valid = valid and record["status"] != "invalid"
                record["realized"] = valid and mounted
                record["status"] = "realized" if record["realized"] else "unfitted" if valid and not mounted else "invalid"

        # A global physical net joins ports even without a Connection edge. The
        # connection checker still decides whether required logical edges exist.
        physical_names: dict[str, str] = {}
        for path, port in ports.items():
            if type(port.net) is str and port.net:
                if port.net in physical_names:
                    groups.join(path, physical_names[port.net])
                else:
                    physical_names[port.net] = path
        edge_names: dict[str, str] = {}
        valid_edges = []
        for connection in design.connections:
            if connection.source not in ports or connection.target not in ports:
                finding("physical_ports.missing_endpoint", "Connection refers to an unknown interface port.", connection.source)
                continue
            groups.join(connection.source, connection.target)
            if connection.net in physical_names:
                groups.join(connection.source, physical_names[connection.net])
            if connection.net in edge_names:
                groups.join(connection.source, edge_names[connection.net])
            else:
                edge_names[connection.net] = connection.source
            valid_edges.append(connection)
        grouped: dict[str, list[str]] = defaultdict(list)
        edge_labels: dict[str, set[str]] = defaultdict(set)
        for path in ports:
            grouped[groups.find(path)].append(path)
        for connection in valid_edges:
            edge_labels[groups.find(connection.source)].add(connection.net)
        for representative, paths in sorted(grouped.items()):
            intentions = {ports[path].net for path in paths if type(ports[path].net) is str and ports[path].net}
            # Preserve completely abstract legacy aliases. Once any intention is
            # present, all labels in this transitive group must be exact; no
            # geometric/net rewrite is inferred from an abstract relay edge.
            labels = intentions | edge_labels[representative]
            if intentions and len(labels) > 1:
                finding("physical_ports.net_conflict", "Connected port intentions and edge labels describe different physical nets.", min(paths),
                        {"ports": sorted(paths), "port_nets": sorted(intentions), "connection_nets": sorted(edge_labels[representative])})
                for path in paths:
                    records[path].update(status="net_conflict", realized=False)

        missing_severity = Severity.ERROR if require_physical_ports else Severity.WARNING
        for path, record in sorted(records.items()):
            if record["realized"]:
                continue
            if record["status"] in ("abstract", "net_only", "unfitted"):
                reason = "Targets include a component that is not fitted." if record["status"] == "unfitted" else "No complete explicit mapping to fitted component pins is declared."
                finding("physical_ports.unrealized", reason, path,
                        {"status": record["status"], "net": record["net"], "require_physical_ports": require_physical_ports}, missing_severity)
        realized = sum(record["realized"] for record in records.values())
        total = len(records)
        complete = total > 0 and realized == total
        report.metadata["coverage"] = {
            "status": "not_applicable" if not total else "complete" if complete else "partial" if realized else "unverified",
            "complete": complete, "require_physical_ports": require_physical_ports,
            "total_ports": total, "realized_ports": realized,
            "abstract_ports": sum(record["status"] == "abstract" for record in records.values()),
            "unrealized_ports": total - realized, "mapped_pins": len(mapped_pins),
            "ports": [records[path] for path in sorted(records)],
        }
        report.metadata["completed"] = True
    except Exception as exc:
        report.add(Issue("physical_ports.incomplete", Severity.BLOCKER,
                         f"Physical interface checks could not complete: {type(exc).__name__}: {exc}"))
        report.metadata["coverage"] = {"status": "unverified", "complete": False,
                                        "require_physical_ports": require_physical_ports}
    return report
