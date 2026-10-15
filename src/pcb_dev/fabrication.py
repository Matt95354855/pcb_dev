"""Conservative planning profiles and auditable assembly tables.

Profiles are editable project assumptions, not supplier capability certification.
"""

from __future__ import annotations

import csv
import io
import math
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .model import Design


@dataclass(frozen=True)
class ManufacturerProfile:
    id: str
    min_track_mm: float
    min_clearance_mm: float
    min_drill_mm: float
    min_annular_ring_mm: float
    copper_edge_mm: float
    supported_layers: tuple[int, ...]
    source: str
    status: str = "planning-only; confirm with manufacturer before ordering"

    def to_dict(self) -> dict:
        return asdict(self)


PROFILES = {
    "jlcpcb": ManufacturerProfile(
        "jlcpcb", 0.2, 0.2, 0.3, 0.15, 0.5, (2, 4),
        "https://jlcpcb.com/capabilities/pcb-capabilities",
    ),
    "pcbway": ManufacturerProfile(
        "pcbway", 0.2, 0.2, 0.3, 0.15, 0.5, (2, 4),
        "https://www.pcbway.com/capabilities.html",
    ),
    "oshpark": ManufacturerProfile(
        "oshpark", 0.2, 0.2, 0.4, 0.15, 0.5, (2, 4),
        "https://docs.oshpark.com/services/",
    ),
}


def track_width_mm(current_a: float, delta_temp_c: float = 10, copper_oz: float = 1) -> float:
    """IPC-2221 external-layer empirical estimate, *not* an IPC-2152 solver."""
    for value in (current_a, delta_temp_c, copper_oz):
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError("Track width arguments must be finite numbers")
    if current_a < 0 or delta_temp_c <= 0 or copper_oz <= 0:
        raise ValueError("Current >= 0, temperature rise > 0, copper weight > 0 required")
    # I = 0.048 * dT**0.44 * A**0.725, area mil², 1 oz ~1.378 mil.
    if current_a == 0:
        return 0.0
    try:
        area_mil2 = (current_a / (0.048 * delta_temp_c**0.44)) ** (1 / 0.725)
        result = area_mil2 / (1.378 * copper_oz) * 0.0254
    except (OverflowError, ZeroDivisionError) as exc:
        raise ValueError("Track-width estimate exceeds numeric limits") from exc
    if not math.isfinite(result):
        raise ValueError("Track-width estimate exceeds numeric limits")
    return result


def ir_drop_v(current_a: float, length_mm: float, width_mm: float, copper_um: float = 35) -> float:
    """20 °C bulk-copper estimate. Does not include vias/connectors/temperature rise."""
    values = (current_a, length_mm, width_mm, copper_um)
    if any(type(v) not in (int, float) or not math.isfinite(v) for v in values):
        raise ValueError("IR-drop arguments must be finite")
    if current_a < 0 or length_mm < 0 or width_mm <= 0 or copper_um <= 0:
        raise ValueError("Invalid trace geometry or current")
    try:
        result = current_a * 1.68e-8 * length_mm / (width_mm * copper_um * 1e-6)
    except (OverflowError, ZeroDivisionError) as exc:
        raise ValueError("IR-drop estimate exceeds numeric limits") from exc
    if not math.isfinite(result):
        raise ValueError("IR-drop estimate exceeds numeric limits")
    return result


def _csv(rows: list[list]) -> str:
    output = io.StringIO(newline="")
    csv.writer(output, lineterminator="\n").writerows(rows)
    return output.getvalue()


def _safe_cell(value: object) -> str:
    """Preserve CSV data while preventing spreadsheet formula execution."""
    text = str(value)
    return "'" + text if text.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")) else text


def bom_csv(design: Design, *, jlcpcb: bool = False) -> str:
    groups: dict[tuple, list[str]] = {}
    for _, block in design.walk_blocks():
        for component in block.components:
            if component.fitted:
                key = (component.value, component.footprint, component.mpn, component.lcsc)
                groups.setdefault(key, []).append(component.reference)
    header = (["Comment", "Designator", "Footprint", "LCSC Part #"] if jlcpcb else
              ["Value", "References", "Footprint", "MPN", "LCSC", "Quantity"])
    rows = [header]
    for (value, footprint, mpn, lcsc), refs in sorted(groups.items()):
        refs_string = ",".join(sorted(refs))
        row = ([value, refs_string, footprint, lcsc] if jlcpcb else
               [value, refs_string, footprint, mpn, lcsc, len(refs)])
        rows.append([_safe_cell(cell) for cell in row])
    return _csv(rows)


def placement_csv(design: Design, *, strict: bool = False) -> str:
    """JLCPCB-column draft from model positions, no rotation correction implied."""
    rows = [["Designator", "Mid X", "Mid Y", "Layer", "Rotation"]]
    missing = []
    for _, block in design.walk_blocks():
        for component in sorted(block.components, key=lambda c: c.reference):
            if not component.fitted:
                continue
            p = component.position
            if p is None:
                missing.append(component.reference)
                continue
            rows.append([_safe_cell(component.reference), f"{p.x_mm:g}mm", f"{p.y_mm:g}mm",
                         "Top" if p.side == "top" else "Bottom", f"{p.rotation % 360:g}"])
    if strict and missing:
        raise ValueError(f"Missing component positions: {', '.join(sorted(missing))}")
    return _csv(rows)
