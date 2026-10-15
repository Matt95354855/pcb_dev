"""Deterministic dependency ordering and change-impact queries."""

from __future__ import annotations

import heapq
from .model import Design, ModelError


def _dependencies(design: Design) -> dict[str, set[str]]:
    blocks = dict(design.walk_blocks())
    dependencies: dict[str, set[str]] = {}
    for path, block in blocks.items():
        deps = set(block.depends_on)
        missing = deps - set(blocks)
        if missing:
            raise ModelError(f"{path}: unknown dependency {sorted(missing)[0]!r}")
        dependencies[path] = deps
    return dependencies


def dependency_order(design: Design) -> list[str]:
    """Topological order of explicit dependencies, ties sorted by block path.

    A block is returned after every block named in its ``depends_on`` list.
    Hierarchical containment is separate from dependency declarations.
    """
    dependencies = _dependencies(design)
    remaining = {path: len(deps) for path, deps in dependencies.items()}
    reverse = {path: set() for path in dependencies}
    for path, deps in dependencies.items():
        for dependency in deps:
            reverse[dependency].add(path)
    ready = [path for path, count in remaining.items() if count == 0]
    heapq.heapify(ready)
    ordered: list[str] = []
    while ready:
        path = heapq.heappop(ready)
        ordered.append(path)
        for dependent in sorted(reverse[path]):
            remaining[dependent] -= 1
            if remaining[dependent] == 0:
                heapq.heappush(ready, dependent)
    if len(ordered) != len(dependencies):
        unresolved = {path for path, count in remaining.items() if count}
        # Iterative path following produces an actual cycle, not just a list
        # that might include downstream victims of the cyclic dependency.
        path = min(unresolved)
        chain: list[str] = []
        positions: dict[str, int] = {}
        while path not in positions:
            positions[path] = len(chain)
            chain.append(path)
            path = min(dependencies[path] & unresolved)
        cycle = chain[positions[path]:] + [path]
        raise ModelError("dependency cycle: " + " -> ".join(cycle))
    return ordered


def dependents(design: Design, path: str) -> list[str]:
    """All transitive explicit dependents, in deterministic build order."""
    order = dependency_order(design)
    dependencies = _dependencies(design)
    if path not in dependencies:
        raise ModelError(f"unknown block path {path!r}")
    affected = {path}
    for candidate in order:
        if dependencies[candidate] & affected:
            affected.add(candidate)
    return [candidate for candidate in order if candidate in affected and candidate != path]
