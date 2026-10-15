"""Repeatable equation-based tolerance exploration, not a SPICE/thermal sign-off."""

from __future__ import annotations

import itertools
import math
import random
import statistics
from typing import Any

from .expressions import evaluate_constraint, resolve_parameters
from .model import Design


def _finite(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def thermal_estimate_c(power_w: float, theta_ja_c_per_w: float, ambient_c: float = 25) -> float:
    """One-resistance steady-state estimate; user supplies a qualified theta_JA."""
    if any(not _finite(v) for v in (power_w, theta_ja_c_per_w, ambient_c)):
        raise ValueError("Thermal inputs must be finite numbers")
    if power_w < 0 or theta_ja_c_per_w <= 0 or ambient_c < -273.15:
        raise ValueError("Invalid thermal inputs")
    result = ambient_c + power_w * theta_ja_c_per_w
    if not math.isfinite(result):
        raise ValueError("Thermal estimate exceeds numeric limits")
    return result


def analyze_tolerances(
    design: Design, block_path: str, relative: dict[str, float], *, samples: int = 1000, seed: int = 0,
) -> dict:
    """Uniform independent Monte Carlo + all parameter corners (up to 8 variables).

    This is a numerical exploration. Nonlinear extrema can occur between corners,
    and actual distributions/correlations must be supplied by engineering analysis.
    """
    if type(samples) is not int or not 1 <= samples <= 10000:
        raise ValueError("samples must be an integer between 1 and 10000")
    if type(seed) is not int:
        raise ValueError("seed must be an integer")
    if not isinstance(relative, dict) or not 1 <= len(relative) <= 8:
        raise ValueError("Select between 1 and 8 tolerance parameters")
    baseline = resolve_parameters(design)
    if block_path not in baseline:
        raise ValueError(f"Unknown block path: {block_path}")
    block = dict(design.walk_blocks())[block_path]
    names = sorted(relative)
    for name in names:
        if name in block.equations:
            raise ValueError(f"{name} is a calculated output; vary an input parameter")
        if name not in baseline[block_path] or not _finite(baseline[block_path][name]):
            raise ValueError(f"{name} is not a finite input parameter")
        if not _finite(relative[name]) or not 0 <= relative[name] <= 1:
            raise ValueError("Relative tolerances must be numbers from 0 to 1")
    intervals = {name: sorted((baseline[block_path][name] * (1 - relative[name]),
                               baseline[block_path][name] * (1 + relative[name]))) for name in names}
    if any(not math.isfinite(value) for values in intervals.values() for value in values):
        raise ValueError("Tolerance intervals exceed numeric limits")

    def run(values: tuple[float, ...]) -> tuple[dict, list[bool]]:
        candidate = Design.from_dict(design.to_dict())
        candidate_block = dict(candidate.walk_blocks())[block_path]
        candidate_block.parameters.update(dict(zip(names, values)))
        resolved = resolve_parameters(candidate)[block_path]
        passed = [evaluate_constraint(item.expression, resolved) for item in block.constraints]
        return resolved, passed

    rng = random.Random(seed)
    observed: dict[str, list[float]] = {}
    failures = [0] * len(block.constraints)
    failed_samples = 0
    for _ in range(samples):
        resolved, passed = run(tuple(rng.uniform(*intervals[name]) for name in names))
        for key, value in resolved.items():
            if _finite(value):
                observed.setdefault(key, []).append(value)
        failed_samples += not all(passed)
        for index, success in enumerate(passed):
            failures[index] += not success
    corners: dict[str, list[float]] = {}
    failed_corners = 0
    for values in itertools.product(*(intervals[name] for name in names)):
        resolved, passed = run(values)
        failed_corners += not all(passed)
        for key, value in resolved.items():
            if _finite(value):
                corners.setdefault(key, []).append(value)
    summary = {}
    for key, values in sorted(observed.items()):
        summary[key] = {"sample_min": min(values), "sample_max": max(values),
                        "sample_mean": statistics.fmean(values),
                        "sample_stddev": statistics.pstdev(values),
                        "corner_min": min(corners[key]), "corner_max": max(corners[key])}
    return {"design_sha256": design.fingerprint(), "block_path": block_path, "seed": seed,
            "samples": samples, "distribution": "independent-uniform", "relative_tolerances": relative,
            "corner_count": 2 ** len(names), "failed_samples": failed_samples,
            "failed_corners": failed_corners, "outputs": summary,
            "constraints": [{"expression": item.expression, "message": item.message,
                             "failed_samples": failures[index]} for index, item in enumerate(block.constraints)],
            "hardware_validated": False, "scope": "equation exploration; corners are not certified bounds"}
