"""Allowlisted engineering tools for the prompt-first project manager.

The host chooses all paths, executables and library bindings. Model calls carry
data only; no tool can review with Claude, waive findings or approve fabrication.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .agent import (AgentHarness, ToolCall, ToolSchema, _arguments, _preserves,
                    _protect_requirements, _validate_operations)
from .checks import check_design
from .model import Design
from .project_store import ProjectError, clone, digest
from .providers import _schema
from .transactions import apply_patch, atomic_write_json


def _object(properties: dict, required=()) -> dict:
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


_TEXT = {"type": "string", "minLength": 1, "maxLength": 4000}
_STRINGS = {"type": "array", "items": _TEXT, "maxItems": 100}
_BRIEF = _object({"title": _TEXT, "summary": _TEXT, "requirements": _STRINGS,
                  "assumptions": _STRINGS, "suggestions": _STRINGS, "questions": _STRINGS},
                 ("title", "summary", "requirements", "assumptions", "suggestions", "questions"))
_STEP = _object({"id": _TEXT, "description": _TEXT, "depends_on": _STRINGS,
                 "tool": _TEXT, "acceptance": _TEXT}, ("id", "description", "depends_on", "tool", "acceptance"))

TOOLS = (
    ToolSchema("read_design_contract", "Read the exact canonical PCB JSON field/type contract and a valid connected example before creating a design. Fixed reference data only, no filesystem arguments.", _object({})),
    ToolSchema("propose_brief", "Propose a brief, separating requirements, assumptions, suggestions and unanswered questions. Only the human can approve its exact hash.", _BRIEF),
    ToolSchema("ask_questions", "Ask bounded clarification questions; stops this turn for the human answer.", _object({"questions": {**_STRINGS, "minItems": 1}}, ("questions",))),
    ToolSchema("propose_plan", "Propose ordered work with explicit dependencies, engineering tools and acceptance evidence.", _object({"steps": {"type": "array", "items": _STEP, "minItems": 1, "maxItems": 100}}, ("steps",))),
    ToolSchema("create_design", "Create the first canonical schema_version=1 design after human brief approval. This is a draft, never engineering approval.", _object({"design": {"type": "object"}}, ("design",))),
    ToolSchema("inspect_design", "Inspect the current canonical design or a named block.", _object({"block_path": _TEXT})),
    ToolSchema("check_design", "Run actual Python metadata and declared physical-port preflight.", _object({})),
    ToolSchema("apply_design_patch", "Apply an atomic guarded patch: add_block, update_block, set_parameter, connect, or update_component {component:absolute_path, changes:{value,pins,position,parameters,mpn,lcsc}}. Existing requirements, component identity, complete pin inventory and frozen blocks remain protected.", _object({"reason": _TEXT, "operations": {"type": "array", "items": {"type": "object"}, "minItems": 1, "maxItems": 32}}, ("reason", "operations"))),
    ToolSchema("build_draft", "Build a real preview or host-configured pinned-library draft. No manufacturing approval.", _object({})),
    ToolSchema("native_check", "Run local KiCad ERC/DRC and native model parity on the latest unchanged draft. Missing tools or evidence block the gate.", _object({})),
    ToolSchema("simulate", "Generate and execute explicit self-contained nominal R/C/L/V/I simulation. No inferred models or hardware qualification.", _object({"analysis": {"type": "object"}, "block_path": _TEXT}, ("analysis",))),
)
_BY_NAME = {tool.name: tool for tool in TOOLS}


def _valid_plan(value: dict) -> None:
    ids = [step["id"] for step in value["steps"]]
    if len(ids) != len(set(ids)):
        raise ProjectError("Plan step identifiers must be unique")
    known: set[str] = set()
    for step in value["steps"]:
        if (len(step["depends_on"]) != len(set(step["depends_on"]))
                or not set(step["depends_on"]) <= known):
            raise ProjectError("Plan dependencies must refer to distinct preceding steps")
        if step["tool"] not in _BY_NAME:
            raise ProjectError("Plan must name an available project tool")
        known.add(step["id"])


def _require_approval(state: dict) -> None:
    brief = state.get("brief")
    if not brief or state.get("approved_brief_hash") != brief.get("hash"):
        raise ProjectError("The human must approve the current brief before engineering tools can modify or build a design")


def _operations(raw):
    # Keep the legacy harness's narrower authority unchanged. The new manager
    # adds one explicit operation, still validated by the transaction engine.
    if type(raw) is not list or not 1 <= len(raw) <= 32:
        raise ProjectError("Patch requires 1..32 operations")
    for operation in raw:
        if isinstance(operation, dict) and operation.get("op") == "update_component":
            if (set(operation) != {"op", "component", "changes"}
                    or not isinstance(operation["component"], str) or not operation["component"].strip()
                    or len(operation["component"]) > 512):
                raise ProjectError("Invalid update_component operation")
        else:
            _validate_operations([operation])
    return raw


def _component_requirements(before, after):
    from .model import walk_components
    components = dict(walk_components(after))
    # Applied conditions and published ratings are evidence declarations, not
    # knobs for making a violation disappear. Electrical model/placement edits
    # remain possible while these existing declarations stay authoritative.
    protected = {"voltage_rating_v", "applied_voltage_v", "current_rating_ma", "applied_current_ma",
                 "power_rating_w", "power_dissipation_w", "derating_factor", "temperature_min_c",
                 "temperature_max_c", "operating_temperature_c"}
    for path, component in walk_components(before):
        current = components.get(path)
        if current is None:
            raise ProjectError("Manager patches cannot remove existing components")
        baseline = {key: value for key, value in component.parameters.items() if key in protected}
        if not _preserves(current.parameters, baseline):
            raise ProjectError("Manager cannot remove or rewrite existing rating and operating-condition declarations")


def _design(state: dict) -> Design:
    if state.get("design") is None:
        raise ProjectError("There is no design yet; propose a brief and obtain human approval first")
    return Design.from_dict(state["design"])


def _preflight(state: dict, design: Design) -> dict:
    state["design"] = design.to_dict()
    state["preflight"] = check_design(design).to_dict()
    # Old engineering results are explicitly stale after any canonical change.
    for key in ("native_evidence", "simulation_evidence"):
        evidence = state.get(key)
        if evidence and evidence.get("design_sha256") != design.fingerprint():
            evidence["stale"] = True
    return state["preflight"]


def _files(store, state: dict, directory: Path, label: str) -> None:
    """Expose only actual bounded, regular files created by a known tool."""
    entries = {item["relative_path"]: item for item in state["artifacts"]}
    for path in sorted(directory.rglob("*")):
        relative = path.relative_to(store.directory).as_posix()
        safe = store.safe_path(relative)
        if safe.is_file():
            if safe.stat().st_size > 64_000_000:
                raise ProjectError("Engineering artifact exceeds the file size limit")
            entries[relative] = {"name": f"{label}: {path.relative_to(directory).as_posix()}", "relative_path": relative}
    if len(entries) > 5000:
        raise ProjectError("Project artifact manifest exceeds its limit")
    state["artifacts"] = list(entries.values())


def _native(design: Design, board: Path, schematic: Path, output: Path, *, bindings=None) -> dict:
    from .backends.kicad import KiCadBackend
    from .parity import check_parity
    from .reports import CheckReport, Issue, gate_reports

    output.mkdir(parents=True)
    preflight = check_design(design)
    backend = KiCadBackend(timeout=60)
    evidence = backend.check(board, schematic, output / "native")
    reports = [preflight]
    for stage in ("erc", "drc"):
        item = evidence[stage]
        completed = bool(item.get("executed") and item.get("native_report") is not None)
        report = CheckReport(stage, metadata={"completed": completed, "native": completed,
                                            "design_hash": design.fingerprint(), "evidence": item})
        if item.get("status") != "passed":
            report.add(Issue(f"{stage}.{item.get('status', 'failed')}", "blocker", item.get("reason") or "Native check failed"))
        reports.append(report)
    exported = backend.export_netlist(schematic, output / "schematic-netlist.xml")
    if exported.get("status") == "passed":
        if bindings is None:
            mapping = check_parity(design, exported["path"], board)
        else:
            from .bindings import load_bindings
            from .bound_parity import check_bound_parity
            mapping = check_bound_parity(load_bindings(design, bindings), exported["path"], board)
        mapping.metadata["native_netlist_export"] = exported
        for condition, code, message in (
            (mapping.metadata.get("netlist_sha256") != exported.get("output_sha256"), "netlist_changed", "Exported netlist changed before parity"),
            (exported.get("input_sha256") != evidence["erc"].get("input_sha256"), "schematic_changed", "Schematic changed between native checks"),
            (mapping.metadata.get("board_sha256") != evidence["drc"].get("input_sha256"), "board_changed", "Board changed between native checks"),
        ):
            if condition:
                mapping.add(Issue("parity." + code, "blocker", message))
    else:
        mapping = CheckReport("model-parity", metadata={"completed": False})
        mapping.add(Issue("parity.netlist_unverified", "blocker", exported.get("reason") or "Native netlist is unavailable"))
    reports.append(mapping)
    gate = gate_reports(reports, required_stages=("preflight", "erc", "drc", "model-parity"), design_hash=design.fingerprint())
    result = {"design_sha256": design.fingerprint(), "status": "passed" if not gate.blocking else "blocked",
              "validation_passed": not gate.blocking, "fabrication_ready": False,
              "reports": [report.to_dict() for report in reports], "gate": gate.to_dict()}
    atomic_write_json(result, output / "checks.json")
    return result


class ProjectTools:
    def __init__(self, store, *, bindings=None):
        self.store = store
        # Configuration belongs to the host, never to a model argument.
        self.bindings = Path(bindings).resolve() if bindings is not None else None

    def execute(self, state: dict, call: ToolCall) -> tuple[dict, bool]:
        if call.name not in _BY_NAME:
            raise ProjectError("Tool is not available to the project manager")
        args = _arguments(call)
        _schema(args, _BY_NAME[call.name].parameters)
        name = call.name
        if name == "read_design_contract":
            return design_contract(), False
        if name == "propose_brief":
            if not args["requirements"]:
                raise ProjectError("A proposed brief requires at least one explicit requirement")
            proposed = clone(args)
            proposed["hash"] = digest(args)
            proposed["approved"] = state.get("approved_brief_hash") == proposed["hash"]
            state["brief"] = proposed
            state["status"] = "awaiting_input" if args["questions"] else "brief_proposed"
            return {"brief": proposed, "human_approval_required": not proposed["approved"]}, False
        if name == "ask_questions":
            if state.get("brief"):
                brief = {key: value for key, value in state["brief"].items() if key not in {"hash", "approved"}}
                brief["questions"] = args["questions"]
                brief["hash"] = digest(brief)
                brief["approved"] = False
                state["brief"] = brief
            state["pending_questions"] = args["questions"]
            state["status"] = "awaiting_input"
            return {"questions": args["questions"]}, True
        if name == "propose_plan":
            _valid_plan(args)
            state["plan"] = clone(args)
            return {"plan": state["plan"], "engineering_proof": False}, False
        if name == "create_design":
            _require_approval(state)
            if state.get("design") is not None:
                raise ProjectError("Existing designs must use guarded atomic patches")
            design = Design.from_dict(args["design"])
            if any(block.frozen or block.frozen_hash is not None for _, block in design.walk_blocks()):
                raise ProjectError("The model cannot introduce freeze records")
            return {"created": True, "report": _preflight(state, design)}, False
        design = _design(state)
        if name == "inspect_design":
            harness = AgentHarness(_NoProvider(), offline=True)
            _, result = harness._tool(design, call)
            return result, False
        if name == "check_design":
            return _preflight(state, design), False
        _require_approval(state)
        if name == "apply_design_patch":
            operations = _operations(args["operations"])
            changed = apply_patch(design, operations, prevent_regressions=True)
            _protect_requirements(design, changed)
            _component_requirements(design, changed)
            return {"applied": changed.fingerprint() != design.fingerprint(), "report": _preflight(state, changed)}, False
        relative = f"artifacts/r{state['revision'] + 1:06d}-{name}"
        directory = self.store.safe_path(relative, create_parent=True)
        if directory.exists():
            raise ProjectError("Tool output already exists; preserve it and retry in a new revision")
        if name == "build_draft":
            from .pipeline import build_preview
            source = self.store.safe_path(f"revisions/r{state['revision'] + 1:06d}-design.json", create_parent=True)
            if source.exists():
                raise ProjectError("Design revision already exists")
            atomic_write_json(design.to_dict(), source)
            result = build_preview(source, directory, bindings=self.bindings)
            lock = json.loads((directory / "build-lock.json").read_text(encoding="utf-8"))
            state["latest_build"] = {"relative_path": relative, "design_sha256": design.fingerprint(), "generated": lock["generated"], "kind": result["kind"]}
            result["output"] = relative
        elif name == "native_check":
            latest = state.get("latest_build")
            if not latest or latest["design_sha256"] != design.fingerprint():
                raise ProjectError("Build the current design before native checks; existing artifacts are missing or stale")
            build = self.store.safe_path(latest["relative_path"])
            generated = latest["generated"]
            board = self.store.safe_path((build.relative_to(self.store.directory) / generated["board"]).as_posix())
            schematic = self.store.safe_path((build.relative_to(self.store.directory) / generated["schematic"]).as_posix())
            captured_bindings = self.store.safe_path((build.relative_to(self.store.directory) / "libraries/bindings.json").as_posix()) if latest.get("kind") == "library-draft" else None
            result = _native(design, board, schematic, directory, bindings=captured_bindings)
            state["native_evidence"] = result
        elif name == "simulate":
            from .backends import NgSpiceBackend, generate_spice
            directory.mkdir(parents=True)
            simulation = design.metadata.get("simulation", {})
            if not isinstance(simulation, dict):
                raise ProjectError("metadata.simulation must be an object")
            generate_spice(design, args["analysis"], directory / "circuit.cir", block_path=args.get("block_path"),
                           ground_nets=simulation.get("ground_nets", ("GND", "0")))
            result = NgSpiceBackend(timeout=60).simulate(directory / "circuit.cir", directory / "native")
            result["design_sha256"] = design.fingerprint()
            result["fabrication_ready"] = False
            atomic_write_json(result, directory / "simulation-evidence.json")
            state["simulation_evidence"] = result
        else:
            raise ProjectError("Tool is not available")
        _files(self.store, state, directory, name)
        return result, False


class _NoProvider:
    network_enabled = False

    def complete(self, *args: Any, **kwargs: Any):
        raise ProjectError("Inspection does not invoke a model")


def design_contract() -> dict:
    """Portable data reference, intentionally independent of repository files."""
    return {
        "schema_version": 1,
        "rules": ["Every structural object rejects unknown fields; JSON numbers must be finite and booleans are not numbers.",
                  "Identifiers contain letters, digits, underscore or hyphen and cannot contain dots. References are globally unique.",
                  "Port and dependency paths are absolute, including the root id. Component pins name exact global nets.",
                  "A port with pin_bindings needs net; targets are existing components/pins in that same block and no physical pin may belong to two ports.",
                  "Port.net and every mapped component pin must agree; connections must agree with the same global net. No net union is inferred.",
                  "All unconnected physical pins must still use an explicit named global net; no implicit ground or inferred library pin type.",
                  "Missing placement and generic library strings are draft metadata. Actual host bindings and native checks remain separate.",
                  "Do not introduce frozen or frozen_hash records. No field or model claim authorizes fabrication."],
        "types": {
            "Design": {"required": ["schema_version", "name", "version", "board", "root"], "fields": {"schema_version": "integer 1", "name": "nonempty string", "version": "nonempty string", "board": "Board", "root": "Block", "connections": "Connection[] default []", "metadata": "finite JSON object default {}"}},
            "Board": {"width_mm": "positive number", "height_mm": "positive number", "layers": "integer 2 or 4, default 2", "manufacturer": "jlcpcb, default jlcpcb"},
            "Block": {"required": ["id"], "fields": {"id": "identifier", "version": "nonempty string default 0.1.0", "description": "string default empty", "parameters": "finite JSON object", "ports": "Port[]", "children": "Block[]", "components": "Component[]", "depends_on": "distinct absolute block paths[]", "equations": "{parameter_identifier: safe numeric arithmetic expression string}", "constraints": "[{expression: safe numeric boolean expression, message: nonempty string}]", "resources": "finite JSON object", "variants": "finite JSON object"}},
            "Port": {"required": ["id", "kind", "direction"], "fields": {"id": "identifier", "kind": "power|ground|digital|analog|differential|bus", "direction": "input|output|bidirectional|passive", "net": "nonempty string or null", "pin_bindings": "[{component:local component id,pin:nonempty pin-number string}]", "voltage_min": "number or null", "voltage_max": "number or null", "max_current_ma": "nonnegative number or null", "current_draw_ma": "nonnegative number default 0", "logic_standard": "string or null", "protocol": "string or null", "required": "boolean default true", "polarity": "string or null", "impedance_ohm": "positive number or null"}},
            "Component": {"required": ["id", "reference", "value", "footprint", "pins"], "fields": {"id": "identifier", "reference": "globally unique nonempty string", "value": "string", "footprint": "string library:footprint (not proof of qualification)", "pins": "{nonempty pin-number string:nonempty global-net string}", "parameters": "finite JSON object; simulation needs explicit spice:{kind:R|C|L|V|I,value:SI number,pins:[positive_pin,negative_pin]}", "mpn": "string", "lcsc": "string", "fitted": "boolean default true", "position": "Position or null"}},
            "Position": {"required": ["x_mm", "y_mm"], "fields": {"x_mm": "number", "y_mm": "number", "rotation": "number degrees default 0", "side": "top|bottom default top"}},
            "Connection": {"source": "absolute existing port path", "target": "absolute existing port path", "net": "nonempty exact global net"},
        },
        "example": {"schema_version": 1, "name": "Contract example only", "version": "0.1.0",
                    "board": {"width_mm": 40, "height_mm": 30, "layers": 2, "manufacturer": "jlcpcb"},
                    "root": {"id": "board", "children": [
                        {"id": "power", "ports": [{"id": "out", "kind": "power", "direction": "output", "voltage_min": 3.3, "voltage_max": 3.3, "max_current_ma": 100, "net": "VCC", "pin_bindings": [{"component": "j1", "pin": "1"}]}],
                         "components": [{"id": "j1", "reference": "J1", "value": "External 3.3 V input", "footprint": "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical", "pins": {"1": "VCC", "2": "GND"}, "position": {"x_mm": 5, "y_mm": 5}}]},
                        {"id": "load", "depends_on": ["board.power"], "ports": [{"id": "vcc", "kind": "power", "direction": "input", "voltage_min": 3, "voltage_max": 3.6, "current_draw_ma": 3.3, "net": "VCC", "pin_bindings": [{"component": "r1", "pin": "1"}]}],
                         "components": [{"id": "r1", "reference": "R1", "value": "1k", "footprint": "Resistor_SMD:R_0603_1608Metric", "pins": {"1": "VCC", "2": "GND"}, "position": {"x_mm": 15, "y_mm": 10}, "parameters": {"spice": {"kind": "R", "value": 1000, "pins": ["1", "2"]}}}]}]},
                    "connections": [{"source": "board.power.out", "target": "board.load.vcc", "net": "VCC"}], "metadata": {"example_only": True}},
    }
