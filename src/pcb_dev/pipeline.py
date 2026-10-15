"""Reproducible preview builds. Manufacturing qualification is a separate gate."""

from __future__ import annotations

import html
import json
import shutil
import tempfile
from importlib.resources import files
from pathlib import Path

from . import __version__
from .artifacts import ArtifactError, atomic_write, json_text, write_manifest
from .checks import check_design
from .fabrication import PROFILES, bom_csv, placement_csv
from .graph import dependency_order
from .model import Design, load_design


class BuildError(ValueError):
    pass


def design_document(design: Design, report) -> str:
    lines = [f"# {html.escape(design.name)}", "", f"Version: {design.version}", "",
             f"Design SHA-256: `{design.fingerprint()}`", "",
             "This is a model preview. Generic symbols and footprints require library validation.",
             "No routed board or fabrication approval is implied.", "",
             "## Block hierarchy", "", "| Block | Version | Components | Dependencies |",
             "| --- | --- | ---: | --- |"]
    for path, block in design.walk_blocks():
        lines.append(f"| {path} | {block.version} | {len(block.components)} | "
                     f"{', '.join(block.depends_on) or '—'} |")
    lines += ["", "## Power interfaces", "",
              "| Port | Direction | Voltage range (V) | Capacity (mA) | Load (mA) |",
              "| --- | --- | --- | ---: | ---: |"]
    for path, block in design.walk_blocks():
        for port in block.ports:
            if port.kind == "power":
                lines.append(f"| {path}.{port.id} | {port.direction} | "
                             f"{port.voltage_min}–{port.voltage_max} | "
                             f"{port.max_current_ma} | {port.current_draw_ma} |")
    lines += ["", "## Validation", "", report.to_markdown()]
    return "\n".join(lines) + "\n"


def build_preview(source: str | Path, output: str | Path) -> dict:
    from .backends import generate
    from .expressions import resolve_parameters

    source, output = Path(source).resolve(), Path(output).absolute()
    if output.is_symlink():
        raise BuildError("Output must not be a symlink")
    output = output.resolve()
    if source.is_relative_to(output):
        raise BuildError("Output cannot contain the source design")
    if output.exists():
        raise BuildError(f"Output already exists: {output}. Choose a new directory.")
    design = load_design(source)
    report = check_design(design)
    if report.blocking:
        raise BuildError("Preflight blocked the build. Run 'pcb-dev check' for the findings.")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{output.name}.", dir=output.parent))
    try:
        raw = design.to_dict()
        atomic_write(temporary / "design.json", json_text(raw))
        atomic_write(temporary / "preflight.json", report.to_json() + "\n")
        atomic_write(temporary / "preflight.md", report.to_markdown())
        atomic_write(temporary / "design.md", design_document(design, report))
        atomic_write(temporary / "bom.csv", bom_csv(design))
        atomic_write(temporary / "bom-jlcpcb.csv", bom_csv(design, jlcpcb=True))
        atomic_write(temporary / "placement-draft.csv", placement_csv(design))
        atomic_write(temporary / "parameters.json", json_text(resolve_parameters(design)))
        atomic_write(temporary / "dependency-order.json", json_text(dependency_order(design)))
        profile = PROFILES.get(design.board.manufacturer)
        if profile is None:
            raise BuildError(f"Unknown manufacturer profile: {design.board.manufacturer}")
        atomic_write(temporary / "manufacturer-profile.json", json_text(profile.to_dict()))
        generated = generate(raw, temporary / "kicad")
        # Do not persist machine-specific absolute output paths in a reproducible build.
        generated = {key: str(Path(path).relative_to(temporary)) for key, path in generated.items()}
        lock = {"schema_version": 1, "pcb_dev_version": __version__,
                "design_sha256": design.fingerprint(), "runtime_dependencies": [],
                "native_checks": {"erc": "not_run", "drc": "not_run"},
                "fabrication_ready": False, "generated": generated,
                "blocks": {path: block.version for path, block in design.walk_blocks()}}
        atomic_write(temporary / "build-lock.json", json_text(lock))
        template = files("pcb_dev").joinpath("web/viewer.html").read_text(encoding="utf-8")
        payload = json.dumps({"design": raw, "report": report.to_dict(), "lock": lock},
                             ensure_ascii=False, allow_nan=False)
        # JSON is embedded as inert data; escape closing script tags and HTML delimiters.
        payload = payload.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
        atomic_write(temporary / "index.html", template.replace("__PCB_DATA__", payload))
        write_manifest(temporary, design_hash=design.fingerprint(), kind="preview")
        if output.exists():
            raise ArtifactError("Output appeared while building; refusing to replace it")
        temporary.rename(output)
        return {"output": str(output), "kind": "preview", "fabrication_ready": False,
                "design_sha256": design.fingerprint(), "preflight": report.to_dict()}
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
