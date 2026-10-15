"""Deterministic preview generation and optional, evidence-bearing tool adapters."""

from .kicad import KiCadBackend, generate
from .runner import ToolResult, ToolRunner
from .routing import FreeroutingBackend
from .simulation import NgSpiceBackend
from .spice import SpiceError, generate_spice

__all__ = ["FreeroutingBackend", "KiCadBackend", "NgSpiceBackend", "SpiceError", "ToolResult", "ToolRunner", "generate", "generate_spice"]
