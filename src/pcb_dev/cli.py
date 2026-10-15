"""Offline CLI. Exit 1 means a validation gate failed; exit 2 means invalid input."""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import sys
from dataclasses import asdict
from pathlib import Path

from . import __version__
from .artifacts import archive_build, atomic_write, json_text, verify_manifest
from .backends import KiCadBackend
from .catalog import ComponentCatalog
from .checks import check_design
from .expressions import resolve_parameters
from .fabrication import PROFILES, track_width_mm
from .graph import dependency_order, dependents
from .model import dump_design, freeze_fingerprint, load_design
from .pipeline import build_preview
from .parity import check_parity
from .reports import CheckReport, Issue, gate_reports


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pcb-dev", description="Hierarchical PCB design and validation")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)
    doctor = sub.add_parser("doctor", help="Inspect optional engineering tools")
    doctor.add_argument("--json", action="store_true")
    doctor.add_argument("--require-kicad", action="store_true")
    check = sub.add_parser("check", help="Validate structure and declared electrical interfaces")
    check.add_argument("design", type=Path)
    check.add_argument("--json", action="store_true")
    check.add_argument("--output", type=Path)
    check.add_argument("--require-physical-ports", action="store_true",
                       help="Require every declared port to have a net and fitted physical pin bindings")
    build = sub.add_parser("build", help="Build a checked model preview, not a fabrication release")
    build.add_argument("design", type=Path)
    build.add_argument("--output", type=Path, required=True)
    build.add_argument("--bindings", type=Path,
                       help="Use complete pinned KiCad library bindings instead of generic symbols/pads")
    build.add_argument("--require-physical-ports", action="store_true",
                       help="Block this draft if any declared port lacks physical pin coverage")
    graph = sub.add_parser("graph", help="Show dependency order and impact of a block change")
    graph.add_argument("design", type=Path)
    graph.add_argument("--impact", metavar="BLOCK_PATH")
    graph.add_argument("--json", action="store_true")
    inspect = sub.add_parser("inspect", help="Show normalized model and resolved parameters")
    inspect.add_argument("design", type=Path)
    freeze = sub.add_parser("freeze", help="Record the context fingerprint of a block")
    freeze.add_argument("design", type=Path)
    freeze.add_argument("block_path")
    freeze.add_argument("--output", type=Path, required=True)
    native = sub.add_parser("native-check", help="Run real KiCad ERC/DRC and combine gate evidence")
    native.add_argument("design", type=Path)
    native.add_argument("--board", type=Path, required=True)
    native.add_argument("--schematic", type=Path, required=True)
    native.add_argument("--output", type=Path, required=True)
    native.add_argument("--kicad-cli", default="kicad-cli")
    native.add_argument("--timeout", type=float, default=120)
    native.add_argument("--json", action="store_true")
    native.add_argument("--bindings", type=Path,
                        help="Compare actual library pin types and explicit no-connect evidence")
    native.add_argument("--require-physical-ports", action="store_true")
    verify = sub.add_parser("verify", help="Verify every file against an artifact manifest")
    verify.add_argument("directory", type=Path)
    archive = sub.add_parser("archive", help="Create a deterministic archive of verified artifacts")
    archive.add_argument("directory", type=Path)
    archive.add_argument("--output", type=Path, required=True)
    profile = sub.add_parser("profile", help="Print a conservative manufacturer planning profile")
    profile.add_argument("name", choices=sorted(PROFILES))
    trace = sub.add_parser("trace-width", help="Estimate external copper width, IPC-2221 formula")
    trace.add_argument("current_a", type=float)
    trace.add_argument("--delta-temp-c", type=float, default=10)
    trace.add_argument("--copper-oz", type=float, default=1)
    catalog = sub.add_parser("catalog", help="Search a local JSON component catalog")
    catalog.add_argument("file", type=Path)
    catalog.add_argument("query", nargs="?", default="")
    catalog.add_argument("--category")
    parity = sub.add_parser("parity", help="Compare declared model, KiCad XML netlist and PCB pin nets")
    parity.add_argument("design", type=Path)
    parity.add_argument("--netlist", type=Path, required=True)
    parity.add_argument("--board", type=Path, required=True)
    parity.add_argument("--json", action="store_true")
    parity.add_argument("--bindings", type=Path,
                        help="Use strict library-aware pin and no-connect parity")
    agent = sub.add_parser("agent-demo", help="Replay offline scripted model replies on a copy of a design")
    agent.add_argument("design", type=Path)
    agent.add_argument("--script", type=Path, required=True)
    agent.add_argument("--objective", default="Review and improve the declared model")
    agent.add_argument("--output", type=Path, required=True)
    agent.add_argument("--max-steps", type=int, default=8)
    agent.add_argument("--max-tokens", type=int, default=64000)
    live_agent = sub.add_parser("agent-run", help="Use an explicitly configured local or remote model on a design copy")
    live_agent.add_argument("design", type=Path)
    live_agent.add_argument("--config", type=Path, required=True)
    live_agent.add_argument("--objective", required=True)
    live_agent.add_argument("--output", type=Path, required=True)
    live_agent.add_argument("--max-steps", type=int, default=8)
    live_agent.add_argument("--max-tokens", type=int, default=64000)
    live_agent.add_argument("--allow-network", action="store_true",
                            help="Explicitly permit the configured remote endpoint")
    tolerance = sub.add_parser("tolerance", help="Explore declared equations with seeded uniform tolerances")
    tolerance.add_argument("design", type=Path)
    tolerance.add_argument("block_path")
    tolerance.add_argument("--relative", action="append", required=True, metavar="PARAMETER=FRACTION")
    tolerance.add_argument("--samples", type=int, default=1000)
    tolerance.add_argument("--seed", type=int, default=0)
    simulate = sub.add_parser("simulate", help="Run a self-contained SPICE file with optional Ngspice")
    simulate.add_argument("netlist", type=Path)
    simulate.add_argument("--output", type=Path, required=True)
    simulate.add_argument("--timeout", type=float, default=120)
    route = sub.add_parser("route", help="Run an explicit pinned Freerouting JAR on a supplied DSN")
    route.add_argument("dsn", type=Path)
    route.add_argument("--jar", type=Path, required=True)
    route.add_argument("--sha256", required=True)
    route.add_argument("--output", type=Path, required=True)
    route.add_argument("--max-passes", type=int, default=100)
    route.add_argument("--threads", type=int, default=1)
    package = sub.add_parser("package", help="Create, pin and inspect data-only block packages")
    pkg_sub = package.add_subparsers(dest="package_command", required=True)
    create = pkg_sub.add_parser("create")
    create.add_argument("design", type=Path)
    create.add_argument("block_path")
    create.add_argument("--name", required=True)
    create.add_argument("--version")
    create.add_argument("--output", type=Path, required=True)
    pin = pkg_sub.add_parser("pin")
    pin.add_argument("file", type=Path)
    pin.add_argument("--lock", type=Path, required=True)
    pkg_inspect = pkg_sub.add_parser("inspect")
    pkg_inspect.add_argument("name")
    pkg_inspect.add_argument("--lock", type=Path, required=True)
    library = sub.add_parser("library", help="Inspect and pin inert KiCad symbol/footprint libraries")
    lib_sub = library.add_subparsers(dest="library_command", required=True)
    lib_inspect = lib_sub.add_parser("inspect")
    lib_inspect.add_argument("file", type=Path)
    register = lib_sub.add_parser("pin")
    register.add_argument("file", type=Path)
    register.add_argument("--name", required=True)
    register.add_argument("--registry", type=Path, required=True)
    lib_verify = lib_sub.add_parser("verify")
    lib_verify.add_argument("name")
    lib_verify.add_argument("--registry", type=Path, required=True)
    spice = sub.add_parser("spice", help="Generate a deterministic netlist from explicitly modeled R/C/L/V/I parts")
    spice.add_argument("design", type=Path)
    spice.add_argument("--output", type=Path, required=True)
    spice.add_argument("--analysis", default="op", help="Named metadata analysis (op/tran/ac/dc) or a JSON file")
    spice.add_argument("--analysis-file", type=Path)
    spice.add_argument("--block", dest="block_path")
    return parser


def _native(args) -> int:
    design = load_design(args.design)
    bound = None
    if args.bindings is not None:
        from .bindings import load_bindings
        bound = load_bindings(design, args.bindings)
    output = args.output.resolve()
    for source in (args.design, args.board, args.schematic, *([args.bindings] if args.bindings else [])):
        if source.resolve().is_relative_to(output):
            raise ValueError("Check output directory cannot contain input files")
    if output.exists():
        raise ValueError("Check output already exists; choose a new directory")
    preflight = check_design(design, require_physical_ports=args.require_physical_ports)
    output.mkdir(parents=True)
    backend = KiCadBackend(executable=args.kicad_cli, timeout=args.timeout)
    evidence = backend.check(args.board, args.schematic, output / "native")
    reports = [preflight]
    for stage, item in evidence.items():
        complete = bool(item.get("executed") and item.get("native_report") is not None)
        report = CheckReport(stage, metadata={
            "completed": complete, "native": complete, "design_hash": design.fingerprint(),
            "evidence": item,
        })
        if item["status"] != "passed":
            report.add(Issue(f"{stage}.{item['status']}", "blocker",
                             item.get("reason") or "Native check failed"))
        reports.append(report)
    exported = backend.export_netlist(args.schematic, output / "schematic-netlist.xml")
    if exported["status"] == "passed":
        if bound is None:
            mapping = check_parity(design, exported["path"], args.board)
        else:
            from .bound_parity import check_bound_parity
            mapping = check_bound_parity(bound, exported["path"], args.board)
        mapping.metadata["native_netlist_export"] = exported
        if exported["input_sha256"] != evidence["erc"].get("input_sha256"):
            mapping.add(Issue("parity.schematic_changed", "blocker",
                              "Schematic changed between ERC and netlist export"))
        if mapping.metadata.get("board_sha256") != evidence["drc"].get("input_sha256"):
            mapping.add(Issue("parity.board_changed", "blocker",
                              "Board changed between DRC and model parity comparison"))
    else:
        mapping = CheckReport("model-parity", metadata={"completed": False})
        mapping.add(Issue("parity.netlist_unverified", "blocker",
                          exported.get("reason") or "Native schematic netlist could not be verified"))
    reports.append(mapping)
    gate = gate_reports(reports, required_stages=("preflight", "erc", "drc", "model-parity"),
                        design_hash=design.fingerprint())
    gate.stage = "native-validation-gate"
    result = {"design_sha256": design.fingerprint(), "fabrication_ready": False,
              "physical_ports_required": args.require_physical_ports,
              "validation_passed": not gate.blocking,
              "qualification_pending": ["physical library geometry and pinout", "DFM",
                                        "supplier capability revision", "Gerber/BOM/CPL review"],
              "reports": [report.to_dict() for report in reports], "gate": gate.to_dict()}
    atomic_write(output / "checks.json", json_text(result))
    atomic_write(output / "checks.md", "\n\n".join(report.to_markdown() for report in [*reports, gate]))
    print(json_text(result) if args.json else gate.to_markdown())
    return 1 if gate.blocking else 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "doctor":
            diagnostic = KiCadBackend().diagnose()
            result = {"pcb_dev": __version__, "python": platform.python_version(),
                      "kicad": diagnostic,
                      "optional": {tool: shutil.which(tool) is not None
                                   for tool in ("ngspice", "java")}}
            if args.json:
                print(json_text(result), end="")
            else:
                print(f"pcb_dev {__version__} · Python {platform.python_version()}")
                print(f"KiCad: {diagnostic.get('version') or diagnostic['status']}")
                print("Model checks and previews work without KiCad. Native ERC/DRC require KiCad 9/10.")
            return int(args.require_kicad and not diagnostic["supported"])
        if args.command == "check":
            design = load_design(args.design)
            report = check_design(design, require_physical_ports=args.require_physical_ports)
            text = report.to_json() + "\n" if args.json else report.to_markdown()
            if args.output:
                if args.output.resolve() == args.design.resolve():
                    raise ValueError("Report output must not overwrite the design")
                atomic_write(args.output, text)
            print(text, end="" if text.endswith("\n") else "\n")
            return int(report.blocking)
        if args.command == "build":
            result = build_preview(args.design, args.output, bindings=args.bindings,
                                   require_physical_ports=args.require_physical_ports)
            label = "Library draft" if result["kind"] == "library-draft" else "Preview"
            print(f"{label} built: {result['output']}\nFabrication approval: pending native checks and physical qualification")
        elif args.command == "graph":
            design = load_design(args.design)
            order = dependency_order(design)
            result = {"build_order": order}
            if args.impact:
                result["affected"] = dependents(design, args.impact)
            print(json_text(result) if args.json else "\n".join(
                f"{key}: {' → '.join(value) or '(none)'}" for key, value in result.items()))
        elif args.command == "inspect":
            design = load_design(args.design)
            print(json_text({"design": design.to_dict(), "parameters": resolve_parameters(design),
                             "sha256": design.fingerprint()}), end="")
        elif args.command == "freeze":
            design = load_design(args.design)
            if args.output.exists():
                raise ValueError("Freeze output already exists; choose a new file")
            block = dict(design.walk_blocks()).get(args.block_path)
            if block is None:
                raise ValueError(f"Unknown block: {args.block_path}")
            block.frozen_hash = freeze_fingerprint(design, args.block_path)
            block.frozen = True
            dump_design(design, args.output)
            print(f"Frozen {args.block_path}: {block.frozen_hash}")
        elif args.command == "native-check":
            return _native(args)
        elif args.command == "verify":
            manifest = verify_manifest(args.directory)
            print(f"Verified {len(manifest['files'])} files · {manifest.get('kind', 'unknown')} artifacts")
        elif args.command == "archive":
            result = archive_build(args.directory, args.output)
            print(f"Archive created: {result}")
        elif args.command == "profile":
            print(json_text(PROFILES[args.name].to_dict()), end="")
        elif args.command == "trace-width":
            result = track_width_mm(args.current_a, args.delta_temp_c, args.copper_oz)
            print(f"Estimated external track width: {result:.3f} mm (IPC-2221 estimate; verify thermally)")
        elif args.command == "catalog":
            catalog = ComponentCatalog.load(args.file)
            print(json_text([asdict(part) for part in catalog.search(args.query, args.category)]), end="")
        elif args.command == "parity":
            design = load_design(args.design)
            if args.bindings is None:
                report = check_parity(design, args.netlist, args.board)
            else:
                from .bindings import load_bindings
                from .bound_parity import check_bound_parity
                report = check_bound_parity(load_bindings(design, args.bindings), args.netlist, args.board)
            print(report.to_json() if args.json else report.to_markdown())
            return int(report.blocking)
        elif args.command in {"agent-demo", "agent-run"}:
            from .agent import AgentHarness, Budget, ModelReply, ScriptedProvider, ToolCall
            from .transactions import read_json
            design = load_design(args.design)
            output = args.output.resolve()
            if output.exists() or args.design.resolve().is_relative_to(output):
                raise ValueError("Agent output must be a new directory outside the source")
            offline, secrets = True, ()
            if args.command == "agent-demo":
                script = read_json(args.script)
                if not isinstance(script, dict) or set(script) != {"replies"} or not isinstance(script["replies"], list):
                    raise ValueError("Agent script requires a replies array")
                replies = []
                for item in script["replies"]:
                    if not isinstance(item, dict) or set(item) - {"content", "tool_calls"}:
                        raise ValueError("Invalid scripted reply")
                    calls = item.get("tool_calls", [])
                    if not isinstance(calls, list):
                        raise ValueError("tool_calls must be an array")
                    replies.append(ModelReply(item.get("content", ""), tuple(ToolCall(**call) for call in calls)))
                provider = ScriptedProvider(replies)
            else:
                from .providers import ChatCompletionsProvider, ProviderConfig
                config = ProviderConfig.from_dict(read_json(args.config))
                provider = ChatCompletionsProvider(config)
                if provider.network_enabled and not args.allow_network:
                    raise ValueError("Remote model requires the explicit --allow-network option")
                offline = not provider.network_enabled
                if config.api_key_env:
                    secrets = (os.environ.get(config.api_key_env, ""),)
            result = AgentHarness(provider, Budget(
                max_steps=args.max_steps, max_tokens=args.max_tokens), offline=offline,
                redaction_secrets=secrets).run(design, args.objective)
            output.mkdir(parents=True)
            dump_design(result.design, output / "proposed-design.json")
            atomic_write(output / "session.json", json_text(result.to_dict()))
            atomic_write(output / "trace.json", result.trace_json())
            print(f"Agent session: {result.status} · proposed design: {output / 'proposed-design.json'}")
            return int(result.final_report.blocking or result.status != "model_finished")
        elif args.command == "tolerance":
            from .analysis import analyze_tolerances
            relative = {}
            for value in args.relative:
                name, separator, fraction = value.partition("=")
                if not separator or not name or name in relative:
                    raise ValueError("Use unique --relative parameter=fraction arguments")
                relative[name] = float(fraction)
            result = analyze_tolerances(load_design(args.design), args.block_path, relative,
                                        samples=args.samples, seed=args.seed)
            print(json_text(result), end="")
            return int(result["failed_samples"] > 0 or result["failed_corners"] > 0)
        elif args.command == "simulate":
            from .backends import NgSpiceBackend
            if args.output.exists():
                raise ValueError("Simulation output must be a new directory")
            result = NgSpiceBackend(timeout=args.timeout).simulate(args.netlist, args.output)
            if args.output.exists():
                atomic_write(args.output / "simulation-evidence.json", json_text(result))
            print(json_text(result), end="")
            return int(result["status"] != "passed")
        elif args.command == "route":
            from .backends import FreeroutingBackend
            if args.output.exists():
                raise ValueError("Routing output must be a new directory")
            result = FreeroutingBackend(args.jar, expected_sha256=args.sha256).route(
                args.dsn, args.output, max_passes=args.max_passes, threads=args.threads)
            if args.output.exists():
                atomic_write(args.output / "routing-evidence.json", json_text(result))
            print(json_text(result), end="")
            return int(result["status"] != "passed")
        elif args.command == "package":
            from .packages import create_package, load_locked_package, lock_package, write_package
            if args.package_command == "create":
                if args.output.exists():
                    raise ValueError("Package output must be a new file")
                package = create_package(load_design(args.design), args.block_path,
                                         name=args.name, version=args.version)
                digest = write_package(package, args.output)
                print(json_text({"name": package.name, "version": package.version,
                                 "sha256": digest, "file": str(args.output)}), end="")
            elif args.package_command == "pin":
                print(json_text(lock_package(args.file, args.lock).to_dict()), end="")
            else:
                print(json_text(load_locked_package(args.lock, args.name).to_dict()), end="")
        elif args.command == "library":
            from .libraries import (check_footprint, load_footprint, load_pinned_library,
                                    load_symbol_library, register_library)
            if args.library_command == "pin":
                pin = register_library(args.file, args.registry, name=args.name)
                print(json_text(pin.to_dict()), end="")
            elif args.library_command == "verify":
                print(json_text(load_pinned_library(args.registry, args.name).to_dict()), end="")
            else:
                if args.file.suffix == ".kicad_mod":
                    inventory = load_footprint(args.file)
                    report = check_footprint(inventory)
                    print(json_text({"inventory": inventory.to_dict(), "checks": report.to_dict()}), end="")
                    return int(report.blocking)
                if args.file.suffix != ".kicad_sym":
                    raise ValueError("Library must be a .kicad_mod or .kicad_sym file")
                print(json_text(load_symbol_library(args.file).to_dict()), end="")
        elif args.command == "spice":
            from .backends import generate_spice
            from .transactions import read_json
            design = load_design(args.design)
            simulation = design.metadata.get("simulation", {})
            if not isinstance(simulation, dict):
                raise ValueError("metadata.simulation must be an object")
            if args.analysis_file or args.analysis not in {"op", "tran", "ac", "dc"}:
                analysis = read_json(args.analysis_file or Path(args.analysis))
            else:
                analyses = simulation.get("analyses", {})
                if not isinstance(analyses, dict):
                    raise ValueError("metadata.simulation.analyses must be an object")
                analysis = analyses.get(args.analysis)
                if analysis is None:
                    if args.analysis == "op":
                        analysis = {"kind": "op"}
                    else:
                        raise ValueError("Requested analysis requires metadata parameters or --analysis-file")
            result = generate_spice(design, analysis, args.output, block_path=args.block_path,
                                    ground_nets=simulation.get("ground_nets", ("GND", "0")))
            print(json_text(result), end="")
        return 0
    except (ValueError, OSError, TypeError, OverflowError) as exc:
        print(f"pcb-dev: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("pcb-dev: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
