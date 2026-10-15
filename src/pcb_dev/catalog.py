"""Offline component selection with explicit provenance and no invented stock."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


class CatalogError(ValueError):
    pass


def _unique_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise CatalogError(f"Duplicate catalog JSON key: {key}")
        result[key] = value
    return result


@dataclass(frozen=True)
class Part:
    id: str
    description: str
    category: str
    footprint: str
    parameters: dict[str, float] = field(default_factory=dict)
    mpn: str = ""
    manufacturer: str = ""
    lcsc: str = ""
    status: str = "review"
    source: str = ""
    unit_price: float | None = None
    stock: int | None = None
    currency: str = "USD"
    alternatives: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Part:
        if not isinstance(data, dict):
            raise CatalogError("A part must be an object")
        allowed = set(cls.__dataclass_fields__)
        if set(data) - allowed:
            raise CatalogError(f"Unknown part fields: {sorted(set(data) - allowed)}")
        for key in ("id", "description", "category", "footprint"):
            if not isinstance(data.get(key), str) or not data[key].strip():
                raise CatalogError(f"Part {key} must be a nonempty string")
        for key in ("mpn", "manufacturer", "lcsc", "source", "currency"):
            if key in data and not isinstance(data[key], str):
                raise CatalogError(f"Part {key} must be a string")
        if data.get("status", "review") not in {"approved", "review", "avoid", "banned"}:
            raise CatalogError("Invalid library status")
        params = data.get("parameters", {})
        if not isinstance(params, dict):
            raise CatalogError("parameters must be an object")
        for key, value in params.items():
            if not isinstance(key, str) or not _finite(value):
                raise CatalogError(f"Invalid numeric parameter: {key}")
        price = data.get("unit_price")
        if price is not None and (not _finite(price) or price < 0):
            raise CatalogError("unit_price must be a finite nonnegative number")
        stock = data.get("stock")
        if stock is not None and (type(stock) is not int or stock < 0):
            raise CatalogError("stock must be a nonnegative integer or null")
        alternatives = data.get("alternatives", [])
        if not isinstance(alternatives, list) or any(
            not isinstance(item, str) or not item for item in alternatives
        ):
            raise CatalogError("alternatives must be a list of part ids")
        return cls(**{**data, "alternatives": tuple(alternatives)})


def _finite(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


class ComponentCatalog:
    def __init__(self, parts: list[Part]):
        self.parts = {part.id: part for part in parts}
        if len(self.parts) != len(parts):
            raise CatalogError("Duplicate catalog part ids")
        for part in parts:
            missing = set(part.alternatives) - self.parts.keys()
            if missing:
                raise CatalogError(f"{part.id}: unknown alternatives {sorted(missing)}")

    @classmethod
    def load(cls, path: str | Path) -> ComponentCatalog:
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=_unique_fields)
        except (OSError, ValueError) as exc:
            raise CatalogError(f"Cannot read catalog: {exc}") from exc
        if not isinstance(data, dict) or set(data) != {"schema_version", "parts"}:
            raise CatalogError("Catalog requires schema_version and parts")
        if type(data["schema_version"]) is not int or data["schema_version"] != 1:
            raise CatalogError("Unsupported catalog schema_version")
        if not isinstance(data["parts"], list):
            raise CatalogError("parts must be an array")
        return cls([Part.from_dict(part) for part in data["parts"]])

    def search(self, query: str = "", category: str | None = None) -> list[Part]:
        query = query.casefold()
        return sorted(
            [part for part in self.parts.values()
             if query in f"{part.id} {part.description} {part.mpn}".casefold()
             and (category is None or category == part.category)],
            key=lambda part: part.id,
        )

    def select(
        self,
        category: str,
        minimum: dict[str, float] | None = None,
        maximum: dict[str, float] | None = None,
        quantity: int = 1,
        require_stock: bool = False,
        approved_only: bool = True,
    ) -> list[Part]:
        """Return eligible parts; missing data never satisfies a requirement."""
        if type(quantity) is not int or quantity < 1:
            raise CatalogError("quantity must be a positive integer")
        for limits in (minimum, maximum):
            if limits is not None and (
                not isinstance(limits, dict) or any(not _finite(v) for v in limits.values())
            ):
                raise CatalogError("Selection bounds must be finite numbers")
        result = []
        for part in self.search(category=category):
            if part.status in {"banned", "avoid"}:
                continue
            if approved_only and part.status != "approved":
                continue
            if require_stock and (part.stock is None or part.stock < quantity):
                continue
            if any(k not in part.parameters or part.parameters[k] < v
                   for k, v in (minimum or {}).items()):
                continue
            if any(k not in part.parameters or part.parameters[k] > v
                   for k, v in (maximum or {}).items()):
                continue
            result.append(part)
        return sorted(result, key=lambda p: (p.unit_price is None, p.unit_price or 0, p.id))

    def fingerprint(self) -> str:
        content = json.dumps(
            [asdict(self.parts[key]) for key in sorted(self.parts)],
            sort_keys=True, separators=(",", ":"), allow_nan=False,
        ).encode()
        return hashlib.sha256(content).hexdigest()
