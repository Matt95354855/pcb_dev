"""KiCad 9/10 preview files and CLI adapters.

Generated footprints and passive pin symbols are explicitly generic previews.
They preserve explicit pin/net mappings, but are neither verified library parts
nor routed production boards.  A successful native check cannot qualify them.

CLI reference: https://docs.kicad.org/10.0/en/cli/cli.html
File format: https://dev-docs.kicad.org/en/file-formats/
"""

from __future__ import annotations

import hashlib
import csv
import io
import json
import math
from pathlib import Path
import re
import tempfile
import uuid
import xml.etree.ElementTree as ET

from ..hierarchy import assert_snapshot, schematic_snapshot

from .runner import ToolRunner

PREVIEW_MARKER = "PCBDEV_PREVIEW_ONLY"
_NAMESPACE = uuid.UUID("ea595973-8900-5fd7-a1b2-a15479c20200")
_MAX_REPORT_BYTES = 16 * 1024 * 1024


def _uuid(value: str) -> str:
    return str(uuid.uuid5(_NAMESPACE, value))


def _q(value: str) -> str:
    # KiCad quoted strings use JSON-compatible quote and backslash escaping.
    if not isinstance(value, str) or any(ord(c) < 32 for c in value):
        raise ValueError("KiCad strings must not contain control characters")
    return json.dumps(value, ensure_ascii=False)


def _num(value) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("KiCad dimensions must be finite numbers")
    return format(value, ".9g")


def _components(block: dict, parent: str = ""):
    path = f"{parent}.{block['id']}" if parent else block["id"]
    for component in block.get("components", []):
        yield f"{path}.{component['id']}", component
    for child in sorted(block.get("children", []), key=lambda child: child["id"]):
        yield from _components(child, path)


def _pin_order(pin: str):
    return (0, int(pin), pin) if pin.isdigit() else (1, 0, pin)


def _lib_name(component: dict) -> str:
    # Pin numbers are not necessarily contiguous or numeric.
    payload = json.dumps(sorted(component["pins"], key=_pin_order), separators=(",", ":"))
    return "Preview_" + hashlib.sha256(payload.encode()).hexdigest()[:16]


def _symbol_library(component: dict) -> str:
    name = _lib_name(component)
    pins = sorted(component["pins"], key=_pin_order)
    half_height = max(2.54, len(pins) * 1.27)
    lines = [f'    (symbol "PCBDev:{name}" (pin_names (offset 0.508))',
             '      (exclude_from_sim no) (in_bom yes) (on_board yes)',
             '      (property "Reference" "U" (at 0 5.08 0) (effects (font (size 1.27 1.27))))',
             f'      (property "Value" "{name}" (at 0 -5.08 0) (effects (font (size 1.27 1.27))))',
             '      (property "Footprint" "" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))',
             '      (property "Datasheet" "" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))',
             f'      (symbol "{name}_0_1" (rectangle (start -5.08 {_num(half_height)}) (end 5.08 {_num(-half_height)})',
             '        (stroke (width 0.254) (type default)) (fill (type background))))',
             f'      (symbol "{name}_1_1"']
    for i, pin in enumerate(pins):
        y = (len(pins) - 1) * 1.27 - i * 2.54
        lines.extend([f'        (pin passive line (at -7.62 {_num(y)} 0) (length 2.54)',
                      f'          (name {_q(pin)} (effects (font (size 1.27 1.27))))',
                      f'          (number {_q(pin)} (effects (font (size 1.27 1.27)))))'])
    lines.append('      )\n    )')
    return "\n".join(lines)


def _schematic(name: str, stem: str, components: list) -> str:
    root_uuid = _uuid(name + ":schematic")
    libraries = {_lib_name(c): c for _, c in components}
    lines = ['(kicad_sch (version 20250114) (generator "pcb_dev") (generator_version "9.0")',
             f'  (uuid {root_uuid}) (paper "A3")',
             f'  (title_block (title {_q(name)}) (comment 1 "{PREVIEW_MARKER}: passive generic symbols; verify real part pin types"))',
             '  (lib_symbols']
    lines.extend(_symbol_library(libraries[n]) for n in sorted(libraries))
    lines.append('  )')
    y = 30.0
    for index, (path, component) in enumerate(components):
        pins = sorted(component["pins"], key=_pin_order)
        # One column avoids accidental net joins between adjacent generic symbols.
        x = 60.0
        symbol_id = _uuid(path + ":symbol")
        ref = component["reference"]
        lines.extend([f'  (symbol (lib_id "PCBDev:{_lib_name(component)}") (at {_num(x)} {_num(y)} 0) (unit 1)',
                      f'    (in_bom yes) (on_board yes) (dnp {"no" if component.get("fitted", True) else "yes"}) (uuid {symbol_id})',
                      f'    (property "Reference" {_q(ref)} (at {_num(x+7.62)} {_num(y-2.54)} 0) (effects (font (size 1.27 1.27)) (justify left)))',
                      f'    (property "Value" {_q(component["value"])} (at {_num(x+7.62)} {_num(y)} 0) (effects (font (size 1.27 1.27)) (justify left)))',
                      f'    (property "Footprint" "PCBDev:GenericPreview_{len(pins)}" (at {_num(x)} {_num(y)} 0) (effects (font (size 1.27 1.27)) (hide yes)))',
                      f'    (property "Datasheet" "" (at {_num(x)} {_num(y)} 0) (effects (font (size 1.27 1.27)) (hide yes)))',
                      f'    (property "RequestedFootprint" {_q(component.get("footprint", ""))} (at {_num(x)} {_num(y)} 0) (effects (font (size 1.27 1.27)) (hide yes)))',
                      f'    (property "PCBDevStatus" "{PREVIEW_MARKER}" (at {_num(x)} {_num(y)} 0) (effects (font (size 1.27 1.27)) (hide yes)))'])
        for pin in pins:
            lines.append(f'    (pin {_q(pin)} (uuid {_uuid(path + ":pin:" + pin)}))')
        lines.append(f'    (instances (project {_q(stem)} (path "/{root_uuid}" (reference {_q(ref)}) (unit 1)))))')
        for i, pin in enumerate(pins):
            py = y - (len(pins) - 1) * 1.27 + i * 2.54
            net = component["pins"][pin]
            if net:
                lines.append(f'  (global_label {_q(net)} (shape passive) (at {_num(x-7.62)} {_num(py)} 0) (effects (font (size 1 1)) (justify right)) (uuid {_uuid(path + ":label:" + pin)}) (property "Intersheetrefs" "${{INTERSHEET_REFS}}" (at {_num(x-7.62)} {_num(py)} 0) (effects (font (size 1 1)) (hide yes))))')
        y += max(15.24, len(pins) * 2.54 + 7.62)
    lines.append(')\n')
    return "\n".join(lines)


def _board(name: str, board: dict, components: list, net_codes: dict, sheet_plan=None) -> str:
    width, height = board["width_mm"], board["height_mm"]
    lines = ['(kicad_pcb (version 20241229) (generator "pcb_dev") (generator_version "9.0")',
             '  (general (thickness 1.6)) (paper "A4")',
             f'  (title_block (title {_q(name)}) (comment 1 "{PREVIEW_MARKER}: unrouted generic footprints"))',
             '  (layers (0 "F.Cu" signal)']
    if board.get("layers", 2) == 4:
        lines.append('    (4 "In1.Cu" signal) (6 "In2.Cu" signal)')
    lines.extend(['    (2 "B.Cu" signal) (15 "B.Paste" user) (13 "F.Paste" user)',
                  '    (7 "B.SilkS" user "B.Silkscreen") (5 "F.SilkS" user "F.Silkscreen")',
                  '    (3 "B.Mask" user) (1 "F.Mask" user) (25 "Edge.Cuts" user)',
                  '    (29 "B.CrtYd" user "B.Courtyard") (31 "F.CrtYd" user "F.Courtyard")',
                  '    (33 "B.Fab" user) (35 "F.Fab" user))',
                  '  (setup (pad_to_mask_clearance 0))',
                  '  (net 0 "")'])
    for net, code in net_codes.items():
        lines.append(f'  (net {code} {_q(net)})')
    lines.append(f'  (gr_rect (start 0 0) (end {_num(width)} {_num(height)}) (stroke (width 0.05) (type solid)) (fill none) (layer "Edge.Cuts") (uuid {_uuid(name+":outline")}))')
    for index, (path, component) in enumerate(components):
        position = component.get("position") or {}
        x = position.get("x_mm", 5.0 + (index % 5) * 10)
        y = position.get("y_mm", 10.0 + (index // 5) * 12)
        rotation = position.get("rotation", 0)
        side = "B" if position.get("side") == "bottom" else "F"
        pins = sorted(component["pins"], key=_pin_order)
        uid = _uuid(path + ":footprint")
        lines.extend([f'  (footprint "PCBDev:GenericPreview_{len(pins)}" (layer "{side}.Cu") (uuid {uid})',
                      f'    (at {_num(x)} {_num(y)} {_num(rotation)})',
                      f'    (descr "{PREVIEW_MARKER}: geometry is generic and unverified")',
                      f'    (property "Reference" {_q(component["reference"])} (at 0 -2 {_num(rotation)}) (layer "{side}.SilkS") (effects (font (size 1 1)){" (justify mirror)" if side == "B" else ""}))',
                      f'    (property "Value" {_q(component["value"])} (at 0 2 {_num(rotation)}) (layer "{side}.Fab") (effects (font (size 1 1)){" (justify mirror)" if side == "B" else ""}))',
                      f'    (property "RequestedFootprint" {_q(component.get("footprint", ""))} (at 0 0 {_num(rotation)}) (layer "{side}.Fab") (hide yes) (effects (font (size 1 1))))',
                      f'    (property "PCBDevStatus" "{PREVIEW_MARKER}" (at 0 0 {_num(rotation)}) (layer "{side}.Fab") (hide yes) (effects (font (size 1 1))))',
                      f'    (path "{sheet_plan.owner(path).instance_path if sheet_plan else "/" + _uuid(name+":schematic")}/{_uuid(path+":symbol")}")',
                      f'    (attr smd{" dnp" if not component.get("fitted", True) else ""})'])
        for i, pin in enumerate(pins):
            px = i * 2.54
            if side == "B":
                px = -px
            net = component["pins"][pin]
            net_clause = f' (net {net_codes[net]} {_q(net)})' if net else ''
            lines.append(f'    (pad {_q(pin)} smd rect (at {_num(px)} 0 {_num(rotation)}) (size 1.2 1.2) (layers "{side}.Cu" "{side}.Paste" "{side}.Mask"){net_clause} (uuid {_uuid(path+":pad:"+pin)}))')
        lines.append('  )')
    lines.append(')\n')
    return "\n".join(lines)


def _netlist(name: str, components: list, net_codes: dict, sheet_plan=None) -> str:
    root = ET.Element("export", version="E")
    design = ET.SubElement(root, "design")
    ET.SubElement(design, "source").text = name
    ET.SubElement(design, "tool").text = "pcb_dev deterministic generic preview"
    comps = ET.SubElement(root, "components")
    for path, component in components:
        comp = ET.SubElement(comps, "comp", ref=component["reference"])
        ET.SubElement(comp, "value").text = component["value"]
        ET.SubElement(comp, "footprint").text = f'PCBDev:GenericPreview_{len(component["pins"])}'
        ET.SubElement(comp, "libsource", lib="PCBDev", part=_lib_name(component), description=PREVIEW_MARKER)
        sheet = sheet_plan.owner(path) if sheet_plan else None
        ET.SubElement(comp, "sheetpath", names=sheet.human_path if sheet else "/",
                      tstamps=sheet.netlist_path if sheet else "/" + _uuid(name + ":schematic") + "/")
        ET.SubElement(comp, "tstamps").text = _uuid(path + ":symbol")
    parts = ET.SubElement(root, "libparts")
    libraries = {_lib_name(c): c for _, c in components}
    for libname, comp in sorted(libraries.items()):
        part = ET.SubElement(parts, "libpart", lib="PCBDev", part=libname)
        pins = ET.SubElement(part, "pins")
        for pin in sorted(comp["pins"], key=_pin_order):
            ET.SubElement(pins, "pin", num=pin, name=pin, type="passive")
    ET.SubElement(root, "libraries")
    nets = ET.SubElement(root, "nets")
    for net, code in net_codes.items():
        node = ET.SubElement(nets, "net", code=str(code), name=net)
        for _, component in components:
            for pin in sorted(component["pins"], key=_pin_order):
                if component["pins"][pin] == net:
                    ET.SubElement(node, "node", ref=component["reference"], pin=pin, pintype="passive")
    ET.indent(root, space="  ")
    return ET.tostring(root, encoding="unicode", xml_declaration=True) + "\n"


def generate(raw_design: dict, output_dir: Path, *, hierarchical: bool = False) -> dict[str, Path]:
    """Write deterministic, self-contained editable previews; no tools are executed.

    Validate the canonical model before calling this low-level generator.
    Output filenames use a safe slug independent of user-supplied paths.
    """
    if type(hierarchical) is not bool:
        raise ValueError("hierarchical must be an explicit host boolean")
    name = raw_design["name"]
    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_")[:80] or "pcb_dev"
    components = sorted(_components(raw_design["root"]), key=lambda item: (item[1]["reference"], item[0]))
    sheet_plan = None
    if hierarchical:
        from ..hierarchy import plan_sheets
        from ..model import Design
        sheet_plan = plan_sheets(Design.from_dict(raw_design), stem, _uuid(name + ":schematic"))
    net_names = sorted({net for _, c in components for net in c["pins"].values() if net})
    net_codes = {net: code for code, net in enumerate(net_names, 1)}
    board = raw_design["board"]
    if board.get("layers", 2) not in (2, 4) or min(float(_num(board["width_mm"])), float(_num(board["height_mm"]))) <= 0:
        raise ValueError("board requires positive finite dimensions and 2 or 4 layers")
    content = {
        "board": (f"{stem}.kicad_pcb", _board(name, board, components, net_codes, sheet_plan)),
        "schematic": (f"{stem}.kicad_sch", _schematic(name, stem, components)),
        "netlist": (f"{stem}.net", _netlist(name, components, net_codes, sheet_plan)),
        "project": (f"{stem}.kicad_pro", json.dumps({"meta": {"version": 1, "filename": f"{stem}.kicad_pro"}, "board": {"design_settings": {"rules": {"min_clearance": 0.2, "min_track_width": 0.25, "min_via_diameter": 0.6, "min_through_hole_diameter": 0.3}}}}, sort_keys=True, indent=2) + "\n"),
        "rules": (f"{stem}.kicad_dru", '(version 1)\n(rule "PCBDev illustrative minimums - verify manufacturer stackup"\n  (constraint clearance (min 0.2mm))\n  (constraint track_width (min 0.25mm)))\n'),
    }
    if sheet_plan:
        from ..hierarchy import assemble_sheet
        from ..sexpr import dumps, parse_quoted
        for sheet in sheet_plan.sheets:
            local = [(path, component) for path, component in components
                     if path.rsplit(".", 1)[0] == sheet.block_path]
            tree = assemble_sheet(parse_quoted(_schematic(name, stem, local)), sheet, sheet_plan)
            content[sheet.key] = (sheet.filename, dumps(tree) + "\n")
    manifest = {"schema_version": 1, "generator": "pcb_dev", "fabrication_ready": False,
                "preview_marker": PREVIEW_MARKER, "routed": False,
                "limitations": ["Generic unverified footprints and passive pin symbols", "No copper routes", "No native ERC or DRC has been executed by generation", "Rule values are illustrative, not manufacturer qualification"],
                "components": [{"path": p, "reference": c["reference"], "requested_footprint": c.get("footprint", ""), "position_explicit": bool(c.get("position"))} for p, c in components],
                "artifacts": {key: {"file": filename, "sha256": hashlib.sha256(text.encode()).hexdigest()} for key, (filename, text) in sorted(content.items())}}
    if sheet_plan:
        manifest["hierarchy"] = sheet_plan.metadata()
    content["manifest"] = ("generation-manifest.json", json.dumps(manifest, sort_keys=True, indent=2, allow_nan=False) + "\n")
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    paths = {}
    for key, (filename, text) in content.items():
        path = destination / filename
        if path.is_symlink():
            raise ValueError(f"refusing to overwrite symlink: {path}")
        path.write_text(text, encoding="utf-8", newline="\n")
        paths[key] = path
    return paths


def _sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON report key: {key}")
        result[key] = value
    return result


def _native_violations(report: dict, stage: str) -> list:
    if not isinstance(report, dict) or not isinstance(report.get("kicad_version"), str):
        raise ValueError("native report is missing kicad_version")
    if not isinstance(report.get("source"), str) or not report["source"] or not isinstance(report.get("date"), str) or not report["date"]:
        raise ValueError("native report is missing required source/date metadata")
    if "$schema" in report and report["$schema"] != f"https://schemas.kicad.org/{stage}.v1.json":
        raise ValueError("unsupported native report schema")
    if "included_severities" in report and (not isinstance(report["included_severities"], list) or set(report["included_severities"]) != {"error", "warning", "exclusion"}):
        raise ValueError("native report does not include all severities")
    if stage == "drc":
        expected = ("violations", "unconnected_items", "schematic_parity")
        # All native collections must be accounted for, including exclusions.
        if any(not isinstance(report.get(key), list) for key in expected):
            raise ValueError("native DRC report is missing required violation collections")
        if report.get("coordinate_units") not in ("mm", "mils", "in"):
            raise ValueError("native DRC report has invalid coordinate units")
        items = [dict(item, collection=key) for key in expected for item in report[key] if isinstance(item, dict)]
        if len(items) != sum(len(report[key]) for key in expected):
            raise ValueError("native DRC report contains a malformed violation")
    else:
        if not isinstance(report.get("sheets"), list) or not report["sheets"]:
            raise ValueError("native ERC report is missing sheets")
        items = []
        for sheet in report["sheets"]:
            if not isinstance(sheet, dict) or not isinstance(sheet.get("violations"), list):
                raise ValueError("native ERC report has a malformed sheet")
            for item in sheet["violations"]:
                if not isinstance(item, dict):
                    raise ValueError("native ERC report has a malformed violation")
                items.append(dict(item, sheet=sheet.get("path", "")))
    for item in items:
        if not all(isinstance(item.get(key), str) for key in ("type", "description", "severity")) or item["severity"] not in ("error", "warning") or not isinstance(item.get("items"), list):
            raise ValueError("native report contains a malformed violation")
        if "excluded" in item and type(item["excluded"]) is not bool:
            raise ValueError("native exclusion marker is not boolean")
    if "ignored_checks" in report:
        if not isinstance(report["ignored_checks"], list):
            raise ValueError("native report has malformed ignored_checks")
        for ignored in report["ignored_checks"]:
            if not isinstance(ignored, dict) or not all(isinstance(ignored.get(key), str) for key in ("key", "description")):
                raise ValueError("native report has a malformed ignored check")
            items.append({"type": "ignored_check", "severity": "error", "description": ignored["description"], "items": [], "ignored_check": ignored})
    # Suppressed/excluded native items stay visible and block the default gate.
    return items


def _validate_export(key: str, files: list[Path], layers: int) -> None:
    """Basic format and completeness checks, not a manufacturing signoff."""
    if any(file.stat().st_size > 128 * 1024 * 1024 for file in files):
        raise ValueError("export artifact exceeds the size limit")
    if key == "gerbers":
        required = {"F_Cu", "B_Cu", "F_Paste", "B_Paste", "F_Mask", "B_Mask", "F_Silkscreen", "B_Silkscreen", "Edge_Cuts"}
        # KiCad uses the canonical layer name in filenames, with Silkscreen
        # rendered as either SilkS or Silkscreen depending on the file version.
        actual = {file.stem.rsplit("-", 1)[-1].replace("SilkS", "Silkscreen") for file in files}
        if layers == 4:
            required.update({"In1_Cu", "In2_Cu"})
        if not required <= actual:
            raise ValueError("Gerber set is missing required copper or technical layers")
        for file in files:
            text = file.read_text(encoding="utf-8", errors="strict")
            if not all(marker in text for marker in ("%FS", "%MO", "M02*")):
                raise ValueError("Gerber artifact is missing format, unit, or end records")
    elif key == "drill":
        for file in files:
            text = file.read_text(encoding="utf-8", errors="strict")
            if not text.lstrip().startswith("M48") or "M30" not in text:
                raise ValueError("Excellon artifact has no header or end record")
    elif key == "positions":
        for file in files:
            reader = csv.DictReader(io.StringIO(file.read_text(encoding="utf-8-sig")))
            if not reader.fieldnames or not {"Ref", "Val", "Package", "PosX", "PosY", "Rot", "Side"} <= set(reader.fieldnames):
                raise ValueError("position CSV has an unsupported header")
            for row in reader:
                if not row["Ref"] or row["Side"].lower() not in ("top", "bottom") or any(not math.isfinite(float(row[key])) for key in ("PosX", "PosY", "Rot")):
                    raise ValueError("position CSV contains an invalid placement")
    elif key == "step":
        for file in files:
            text = file.read_text(encoding="utf-8", errors="strict")
            if not text.lstrip().startswith("ISO-10303-21;") or "END-ISO-10303-21;" not in text:
                raise ValueError("STEP artifact is missing its exchange header/end record")
    elif key == "schematic_svg":
        for file in files:
            if ET.parse(file).getroot().tag.rsplit("}", 1)[-1] != "svg":
                raise ValueError("schematic export is not an SVG document")


class KiCadBackend:
    """Optional KiCad 9/10 CLI with native output validation and input hashes."""

    def __init__(self, runner: ToolRunner | None = None, executable: str = "kicad-cli", timeout: float = 120):
        self.runner = runner or ToolRunner()
        self.executable = executable
        self.timeout = timeout

    def diagnose(self) -> dict:
        with tempfile.TemporaryDirectory(prefix="pcb-dev-version-") as directory:
            result = self.runner.run(self.executable, ["--version"], Path(directory), timeout=min(self.timeout, 15))
        match = re.search(r"(?<!\d)(\d+)\.(\d+)(?:\.[\w.+-]+)?", result.stdout)
        version = match.group(0) if match else None
        supported = result.status == "success" and bool(match) and int(match.group(1)) in (9, 10)
        return {"available": result.status != "unavailable", "supported": supported,
                "version": version, "status": "passed" if supported else ("unavailable" if result.status == "unavailable" else "failed"),
                "reason": "" if supported else "KiCad CLI 9 or 10 is required", "tool": result.to_dict()}

    def _check_one(self, stage: str, input_file: Path, output_dir: Path, diagnostic: dict) -> dict:
        supplied = Path(input_file).absolute()
        input_file = supplied.resolve()
        report_path = output_dir / f"{stage}.json"
        evidence = {"stage": stage, "status": diagnostic["status"], "executed": False,
                    "input_sha256": _sha256(input_file) if stage != "erc" and input_file.is_file() else None,
                    "tool_version": diagnostic["version"], "report_path": str(report_path),
                    "violations": [], "returncode": None}
        if not input_file.is_file():
            return dict(evidence, status="failed", reason="Input artifact is missing")
        snapshot = None
        if stage == "erc":
            try:
                snapshot = schematic_snapshot(supplied)
                evidence.update(snapshot)
            except (OSError, ValueError, UnicodeError) as exc:
                return dict(evidence, status="failed", reason=f"Invalid schematic input closure: {exc}")
        if not diagnostic["supported"]:
            return dict(evidence, reason=diagnostic["reason"])
        # A fresh isolated report directory prevents stale reports from passing.
        with tempfile.TemporaryDirectory(prefix=f".{stage}-", dir=output_dir) as temporary:
            fresh_report = Path(temporary) / f"{stage}.json"
            mode = "pcb" if stage == "drc" else "sch"
            result = self.runner.run(self.executable, [mode, stage, "--format", "json", "--severity-all", "--exit-code-violations", "--output", str(fresh_report), str(input_file)], input_file.parent, timeout=self.timeout)
            evidence.update(executed=result.executed, returncode=result.returncode, tool=result.to_dict())
            if result.status == "unavailable":
                return dict(evidence, status="unavailable", reason="KiCad CLI became unavailable")
            if result.status == "timeout" or result.returncode not in (0, 5):
                return dict(evidence, status="failed", reason="KiCad check did not complete successfully")
            try:
                if not fresh_report.is_file() or fresh_report.is_symlink() or not 0 < fresh_report.stat().st_size <= _MAX_REPORT_BYTES:
                    raise ValueError("native report is missing, empty, or oversized")
                raw = fresh_report.read_bytes()
                report = json.loads(raw, object_pairs_hook=_unique_object,
                                    parse_constant=lambda value: (_ for _ in ()).throw(ValueError("nonfinite JSON")))
                violations = _native_violations(report, stage)
                source = Path(report["source"])
                if (source.is_absolute() and source.resolve() != input_file) or (not source.is_absolute() and ((len(source.parts) == 1 and source.name != input_file.name) or (len(source.parts) > 1 and (input_file.parent / source).resolve() != input_file))):
                    raise ValueError("native report refers to another source artifact")
                if report["kicad_version"].split(".")[0] != str(diagnostic["version"]).split(".")[0]:
                    raise ValueError("native report version does not match installed KiCad")
                if snapshot is not None:
                    assert_snapshot(supplied, snapshot)
                elif not input_file.is_file() or _sha256(input_file) != evidence["input_sha256"]:
                    raise ValueError("input artifact changed while checks were running")
                if report_path.is_symlink():
                    raise ValueError("refusing to overwrite report symlink")
                report_path.write_bytes(raw)
            except (OSError, ValueError, TypeError) as exc:
                return dict(evidence, status="failed", reason=f"Invalid native check evidence: {exc}")
        evidence.update(violations=violations, report_sha256=_sha256(report_path), native_report=report)
        return dict(evidence, status="passed" if result.returncode == 0 and not violations else "failed",
                    reason="" if result.returncode == 0 and not violations else "Native check reported violations or exclusions")

    def check(self, board: Path, schematic: Path, output_dir: Path) -> dict:
        output_dir = Path(output_dir).resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        diagnostic = self.diagnose()
        return {"erc": self._check_one("erc", schematic, output_dir, diagnostic),
                "drc": self._check_one("drc", board, output_dir, diagnostic)}

    def export_netlist(self, schematic: Path, output: Path) -> dict:
        """Bind native XML to the entire safe schematic dependency closure."""
        supplied = Path(schematic).absolute()
        source, target = supplied.resolve(), Path(output).absolute()
        evidence = {"stage": "native_netlist", "status": "failed", "executed": False,
                    "input_sha256": None,
                    "output_sha256": None, "path": str(target), "returncode": None}
        if not source.is_file():
            return dict(evidence, reason="Input schematic is missing")
        try:
            snapshot = schematic_snapshot(supplied)
            evidence.update(snapshot)
        except (OSError, ValueError, UnicodeError) as exc:
            return dict(evidence, reason=f"Invalid schematic input closure: {exc}")
        if target.is_symlink():
            return dict(evidence, reason="Netlist destination is a symlink")
        if target.resolve() == source:
            return dict(evidence, reason="Netlist output must not overwrite the source schematic")
        dependencies = [source.parent / name for name in snapshot["input_closure"]["files"]]
        if any(target.resolve() == path or (target.exists() and target.samefile(path)) for path in dependencies):
            return dict(evidence, reason="Netlist output must not overwrite any schematic dependency")
        diagnostic = self.diagnose()
        evidence["tool_version"] = diagnostic["version"]
        if not diagnostic["supported"]:
            return dict(evidence, status=diagnostic["status"], reason=diagnostic["reason"])
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".netlist-", dir=target.parent) as directory:
            fresh = Path(directory) / "native.net"
            tool = self.runner.run(self.executable, ["sch", "export", "netlist", "--format", "kicadxml", "--output", str(fresh), str(source)], source.parent, timeout=self.timeout)
            evidence.update(executed=tool.executed, returncode=tool.returncode, tool=tool.to_dict())
            if tool.status != "success":
                return dict(evidence, status="unavailable" if tool.status == "unavailable" else "failed", reason="KiCad netlist export did not complete successfully")
            try:
                if not fresh.is_file() or fresh.is_symlink() or not 0 < fresh.stat().st_size <= _MAX_REPORT_BYTES:
                    raise ValueError("Native XML netlist is missing, empty, or oversized")
                raw = fresh.read_bytes()
                if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
                    raise ValueError("XML declarations and entities are forbidden")
                document = ET.fromstring(raw)
                if document.tag != "export" or document.find("components") is None or document.find("nets") is None:
                    raise ValueError("XML is not a KiCad export with components and nets")
                native_source = document.findtext("design/source")
                if native_source and Path(native_source).name != source.name:
                    raise ValueError("Native netlist refers to a different schematic")
                assert_snapshot(supplied, snapshot)
                if target.is_symlink():
                    raise ValueError("Netlist destination became a symlink")
                target.write_bytes(raw)
            except (OSError, ValueError, ET.ParseError) as exc:
                return dict(evidence, reason=f"Invalid native netlist evidence: {exc}")
        return dict(evidence, status="passed", reason="", output_sha256=_sha256(target))

    def export(self, board: Path, schematic: Path, output_dir: Path, layers: int = 2) -> dict:
        """Export only through KiCad; products remain preview unless separately qualified.

        Returns per-product statuses and paths. All exports use fresh directories;
        a zero tool exit without a nonempty expected artifact is a failure.
        """
        if layers not in (2, 4):
            raise ValueError("layers must be 2 or 4")
        supplied_schematic = Path(schematic).absolute()
        board, schematic = Path(board).resolve(), supplied_schematic.resolve()
        output_dir = Path(output_dir).resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        diagnostic = self.diagnose()
        copper = "F.Cu,B.Cu" if layers == 2 else "F.Cu,In1.Cu,In2.Cu,B.Cu"
        products = {}
        specifications = [
            ("gerbers", board, ["pcb", "export", "gerbers", "--no-protel-ext", "--layers", copper + ",F.Paste,B.Paste,F.Mask,B.Mask,F.SilkS,B.SilkS,Edge.Cuts"], None, ("*.gbr",)),
            ("drill", board, ["pcb", "export", "drill", "--format", "excellon"], None, ("*.drl",)),
            ("positions", board, ["pcb", "export", "pos", "--format", "csv", "--units", "mm", "--side", "both", "--exclude-dnp"], "positions.csv", ("*.csv",)),
            ("step", board, ["pcb", "export", "step", "--force"], "board.step", ("*.step",)),
            ("schematic_svg", schematic, ["sch", "export", "svg", "--exclude-drawing-sheet"], None, ("*.svg",)),
        ]
        for key, source, args, filename, patterns in specifications:
            item = {"status": "failed", "executed": False, "paths": [], "tool_version": diagnostic["version"], "input_sha256": _sha256(source) if key != "schematic_svg" and source.is_file() else None}
            if not source.is_file():
                products[key] = dict(item, reason="Input artifact is missing")
                continue
            snapshot = None
            if key == "schematic_svg":
                try:
                    snapshot = schematic_snapshot(supplied_schematic)
                    item.update(snapshot)
                except (OSError, ValueError, UnicodeError) as exc:
                    products[key] = dict(item, reason=f"Invalid schematic input closure: {exc}")
                    continue
            if not diagnostic["supported"]:
                products[key] = dict(item, status=diagnostic["status"], reason=diagnostic["reason"])
                continue
            with tempfile.TemporaryDirectory(prefix=f".{key}-", dir=output_dir) as directory:
                temp = Path(directory)
                target = temp / filename if filename else temp
                result = self.runner.run(self.executable, [*args, "--output", str(target), str(source)], source.parent, timeout=self.timeout)
                files = sorted({path for pattern in patterns for path in temp.glob(pattern) if path.is_file() and not path.is_symlink() and path.stat().st_size > 0})
                item.update(executed=result.executed, returncode=result.returncode, tool=result.to_dict())
                if result.status != "success" or not files:
                    products[key] = dict(item, reason="Tool failed, source changed, or expected artifact is missing")
                    continue
                try:
                    if snapshot is not None:
                        assert_snapshot(supplied_schematic, snapshot)
                        if len(files) != len(snapshot["input_closure"]["files"]):
                            raise ValueError("Schematic SVG export did not produce one file for every sheet")
                    elif _sha256(source) != item["input_sha256"]:
                        raise ValueError("Source changed during native export")
                    _validate_export(key, files, layers)
                except (OSError, ValueError, KeyError, TypeError, ET.ParseError) as exc:
                    products[key] = dict(item, reason=f"Invalid export artifacts: {exc}")
                    continue
                destination = output_dir / key
                if destination.is_symlink():
                    products[key] = dict(item, reason="Destination is a symlink")
                    continue
                destination.mkdir(exist_ok=True)
                paths = []
                for file in files:
                    dest = destination / file.name
                    if dest.is_symlink():
                        raise ValueError("refusing to overwrite export symlink")
                    dest.write_bytes(file.read_bytes())
                    paths.append(str(dest))
                products[key] = dict(item, status="passed", reason="", paths=paths,
                                     sha256={str(path): _sha256(Path(path)) for path in paths})
        return {"status": "passed" if all(item["status"] == "passed" for item in products.values()) else "failed",
                "fabrication_ready": False, "products": products}
