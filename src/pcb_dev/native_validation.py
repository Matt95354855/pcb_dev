"""Shared native evidence gate for the CLI and host-controlled project tools."""

from __future__ import annotations

from pathlib import Path

from .artifacts import atomic_write, json_text
from .checks import check_design
from .model import Design
from .parity import check_parity
from .reports import CheckReport, Issue, gate_reports


def run_native_validation(design: Design, board: Path, schematic: Path, output: Path, *,
                          bindings=None, backend=None, require_physical_ports: bool = False) -> tuple[dict, str]:
    """Execute native checks and tie every evidence stage to the same input bytes.

    The host chooses all inputs and executable configuration. The gate confirms
    only its listed stages; physical qualification and fabrication stay pending.
    """
    from .backends.kicad import KiCadBackend

    board, schematic, output = Path(board), Path(schematic), Path(output)
    for path in (board, schematic):
        if path.resolve().is_relative_to(output.resolve()):
            raise ValueError("Check output directory cannot contain input files")
    if output.exists() or output.is_symlink():
        raise ValueError("Check output already exists; choose a new directory")
    bound = None
    if bindings is not None:
        from .bindings import BoundDesign, load_bindings
        bound = bindings if isinstance(bindings, BoundDesign) else load_bindings(design, bindings)
        if bound.design.fingerprint() != design.fingerprint():
            raise ValueError("Pinned bindings refer to a different design")
    preflight = check_design(design, require_physical_ports=require_physical_ports)
    output.mkdir(parents=True)
    backend = backend if backend is not None else KiCadBackend(timeout=60)
    evidence = backend.check(board, schematic, output / "native")
    reports = [preflight]
    for stage in ("erc", "drc"):
        item = evidence[stage]
        complete = bool(item.get("executed") and item.get("native_report") is not None)
        report = CheckReport(stage, metadata={"completed": complete, "native": complete,
                                            "design_hash": design.fingerprint(), "evidence": item})
        if item.get("status") != "passed":
            report.add(Issue(f"{stage}.{item.get('status', 'failed')}", "blocker",
                             item.get("reason") or "Native check failed"))
        reports.append(report)
    exported = backend.export_netlist(schematic, output / "schematic-netlist.xml")
    if exported.get("status") == "passed":
        if bound is None:
            mapping = check_parity(design, exported["path"], board)
        else:
            from .bound_parity import check_bound_parity
            mapping = check_bound_parity(bound, exported["path"], board)
        mapping.metadata["native_netlist_export"] = exported
        for changed, code, message in (
            (mapping.metadata.get("netlist_sha256") != exported.get("output_sha256"), "netlist_changed", "Exported netlist changed before model parity comparison"),
            (exported.get("input_sha256") != evidence["erc"].get("input_sha256"), "schematic_changed", "Schematic file set changed between ERC and netlist export"),
            (mapping.metadata.get("board_sha256") != evidence["drc"].get("input_sha256"), "board_changed", "Board changed between DRC and model parity comparison"),
        ):
            if changed:
                mapping.add(Issue("parity." + code, "blocker", message))
        try:
            from .hierarchy import schematic_snapshot
            current = schematic_snapshot(schematic)
            if current["input_sha256"] != exported.get("input_sha256"):
                mapping.add(Issue("parity.schematic_changed_after_export", "blocker",
                                  "Schematic file set changed after native netlist export"))
            mapping.metadata["schematic_input"] = current
        except (ValueError, OSError) as exc:
            mapping.add(Issue("parity.schematic_unverified", "blocker",
                              "Current schematic file set could not be verified", details={"error_type": type(exc).__name__}))
    else:
        mapping = CheckReport("model-parity", metadata={"completed": False})
        mapping.add(Issue("parity.netlist_unverified", "blocker",
                          exported.get("reason") or "Native schematic netlist could not be verified"))
    reports.append(mapping)
    required = ["preflight", "erc", "drc", "model-parity"]
    decoupling = preflight.metadata.get("decoupling", {})
    if decoupling.get("rule_count", 0):
        from .decoupling import check_layout
        layout = check_layout(design, board, bound)
        if layout.metadata.get("board_sha256") != evidence["drc"].get("input_sha256"):
            layout.add(Issue("decoupling.board_changed", "blocker",
                             "PCB bytes used for decoupling differ from native DRC input"))
        reports.append(layout)
        required.append("decoupling-layout")
    gate = gate_reports(reports, required_stages=required, design_hash=design.fingerprint())
    gate.stage = "native-validation-gate"
    result = {"design_sha256": design.fingerprint(), "fabrication_ready": False,
              "physical_ports_required": require_physical_ports,
              "status": "blocked" if gate.blocking else "passed", "validation_passed": not gate.blocking,
              "qualification_pending": ["physical library geometry and pinout", "DFM", "supplier capability revision", "Gerber/BOM/CPL review"],
              "reports": [report.to_dict() for report in reports], "gate": gate.to_dict()}
    atomic_write(output / "checks.json", json_text(result))
    atomic_write(output / "checks.md", "\n\n".join(report.to_markdown() for report in [*reports, gate]))
    return result, gate.to_markdown()
