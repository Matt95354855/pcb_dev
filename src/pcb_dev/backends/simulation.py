"""Optional Ngspice batch runner for self-contained circuit descriptions.

External model includes and interactive control scripts are deliberately rejected.
Callers supply real SPICE models; no electrical behaviour is inferred from generic
PCB preview symbols. This adapter reports engine output, not hardware validation.
"""

from __future__ import annotations

import hashlib
import math
from pathlib import Path
import re
import tempfile

from .runner import ToolRunner

_MAX_BYTES = 64 * 1024 * 1024
_DIRECTIVES = frozenset({".end", ".op", ".tran", ".ac", ".dc", ".print", ".plot", ".save", ".measure", ".meas", ".param", ".options", ".model", ".subckt", ".ends", ".ic", ".nodeset", ".temp", ".func", ".global"})


def _validate_netlist(text: str) -> list[str]:
    if "\x00" in text or len(text.encode()) > _MAX_BYTES:
        raise ValueError("SPICE input is invalid or oversized")
    lines = text.splitlines()
    if len(lines) < 3:
        raise ValueError("SPICE input requires a title, circuit, analysis, and .end")
    analyses, measurements, ended = 0, [], False
    for line in lines[1:]:
        body = line.split(";", 1)[0].strip()
        if not body or body.startswith("*"):
            continue
        lower = body.lower()
        if ended:
            raise ValueError("data after .end is not supported")
        if any(token in body for token in ("`", "$", "!")) or re.search(r"\b(?:file|shell|source|osdi|codemodel)\b", lower):
            raise ValueError("external commands, file-backed sources, and dynamic models are not supported")
        token = lower.split()[0]
        if token.startswith("."):
            if token not in _DIRECTIVES:
                raise ValueError(f"unsupported SPICE directive: {token}")
            analyses += token in {".op", ".tran", ".ac", ".dc"}
            if token in {".measure", ".meas"}:
                fields = body.split()
                if len(fields) < 4 or fields[1].lower() not in {"tran", "ac", "dc"} or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", fields[2]):
                    raise ValueError("measurement requires analysis, a simple name, and expression")
                measurements.append(fields[2].lower())
            ended = token == ".end"
        elif body[0] != "+" and body[0].upper() not in "RCLVIDQMXEFGHBKST":
            raise ValueError("unsupported SPICE device or command")
    if not ended or analyses == 0:
        raise ValueError("SPICE input requires a supported analysis and .end")
    if len(measurements) != len(set(measurements)):
        raise ValueError("SPICE measurement names must be unique")
    return measurements


def _waveform_netlist(text: str) -> str:
    """Ngspice's batch -r cannot evaluate .meas; remove only those cards."""
    lines, skip = [], False
    for line in text.splitlines():
        body = line.lstrip()
        if re.match(r"(?i)^\.meas(?:ure)?\s", body):
            skip = True
            continue
        if skip and (body.startswith("+") or not body or body.startswith("*")):
            continue
        skip = False
        lines.append(line)
    return "\n".join(lines) + "\n"


def _measurement_netlist(text: str) -> str:
    """Run a second batch without -r, with .print to retain analysis vectors."""
    analyses = sorted(set(re.findall(r"(?im)^\s*\.meas(?:ure)?\s+(tran|ac|dc)\b", text)))
    node = None
    for line in text.splitlines()[1:]:
        fields = line.split()
        if len(fields) >= 3 and fields[0][0].upper() in "RCLVIDQMXEFGHBKST":
            node = next((field for field in fields[1:3] if field != "0" and re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.:#/+\-]*", field)), None)
            if node:
                break
    if not node:
        raise ValueError("measurement batch requires a simple non-ground circuit node")
    prints = "\n".join(f".print {analysis} v({node})" for analysis in analyses)
    return re.sub(r"(?im)^\s*\.end\s*$", prints + "\n.end", text) + "\n"


def _validate_raw(path: Path) -> dict:
    if not path.is_file() or path.is_symlink() or not 0 < path.stat().st_size <= _MAX_BYTES:
        raise ValueError("Ngspice produced no bounded waveform artifact")
    with path.open("rb") as stream:
        header = stream.read(min(path.stat().st_size, 65536))
    marker = re.search(rb"(?m)^(?:Binary|Values):\s*\r?\n", header)
    if not marker:
        raise ValueError("Ngspice waveform has no data section")
    prefix = header[:marker.start()].decode("utf-8", errors="strict")
    variables = re.search(r"(?m)^No\. Variables:\s*(\d+)\s*$", prefix)
    points = re.search(r"(?m)^No\. Points:\s*(\d+)\s*$", prefix)
    plot = re.search(r"(?m)^Plotname:\s*(.+)$", prefix)
    if not variables or not points or not plot or int(variables[1]) < 1 or int(points[1]) < 1 or path.stat().st_size <= marker.end():
        raise ValueError("Ngspice waveform header/data is incomplete")
    return {"plot": plot[1].strip(), "variables": int(variables[1]), "points": int(points[1])}


class NgSpiceBackend:
    """Execute a local, self-contained SPICE netlist through ``ngspice -n -b``."""

    def __init__(self, runner: ToolRunner | None = None, executable: str = "ngspice", timeout: float = 120):
        self.runner = runner or ToolRunner()
        self.executable = executable
        self.timeout = timeout

    def diagnose(self) -> dict:
        with tempfile.TemporaryDirectory(prefix="pcb-dev-ngspice-version-") as directory:
            result = self.runner.run(self.executable, ["--version"], Path(directory), timeout=min(self.timeout, 15))
        match = re.search(r"ngspice[- ]+(\d+(?:\.\d+)?)", result.stdout + result.stderr, flags=re.I)
        return {"status": "passed" if result.status == "success" and match else ("unavailable" if result.status == "unavailable" else "failed"),
                "version": match[1] if match else None, "tool": result.to_dict()}

    def simulate(self, netlist: Path, output_dir: Path) -> dict:
        """Return waveform/log paths, hashes and requested numeric measurements.

        ``passed`` means Ngspice completed and emitted a waveform. It does not
        certify any board, model fidelity, voltage range, or acceptance limits.
        """
        source = Path(netlist).resolve()
        result = {"stage": "simulation", "status": "failed", "executed": False,
                  "hardware_validated": False, "measurements": {}, "paths": []}
        if not source.is_file() or not 0 < source.stat().st_size <= _MAX_BYTES:
            return dict(result, reason="SPICE input is missing, empty, or oversized")
        raw_input = source.read_bytes()
        result["input_sha256"] = hashlib.sha256(raw_input).hexdigest()
        try:
            names = _validate_netlist(raw_input.decode("utf-8"))
        except (ValueError, UnicodeError) as exc:
            return dict(result, reason=f"Invalid self-contained SPICE input: {exc}")
        diagnostic = self.diagnose()
        result["tool_version"] = diagnostic["version"]
        if diagnostic["status"] != "passed":
            return dict(result, status=diagnostic["status"], reason="A working Ngspice executable is required")
        destination = Path(output_dir).resolve()
        destination.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".ngspice-", dir=destination) as directory:
            working = Path(directory)
            copied = working / "circuit.cir"
            original_text = raw_input.decode("utf-8")
            copied.write_text(_waveform_netlist(original_text), encoding="utf-8")
            waveform, log = working / "simulation.raw", working / "simulation.log"
            tool = self.runner.run(self.executable, ["-n", "-b", "-r", str(waveform), "-o", str(log), str(copied)], working, timeout=self.timeout)
            result.update(executed=tool.executed, returncode=tool.returncode, tool=tool.to_dict())
            if tool.status != "success":
                return dict(result, status="unavailable" if tool.status == "unavailable" else "failed", reason="Ngspice did not complete successfully")
            try:
                metadata = _validate_raw(waveform)
                if not log.is_file() or log.is_symlink() or not 0 < log.stat().st_size <= _MAX_BYTES:
                    raise ValueError("Ngspice log is missing, empty, or oversized")
                text = log.read_text(encoding="utf-8", errors="replace")
                if re.search(r"(?im)^\s*(?:fatal|error\b|doAnalyses:)|(?:simulation|analysis).*aborted|failed to converge", text):
                    raise ValueError("Ngspice log reports an error or failed analysis")
                measurements = {}
                extra_artifacts = [copied]
                if names:
                    measurement_input, measurement_log = working / "measurements.cir", working / "measurements.log"
                    measurement_input.write_text(_measurement_netlist(original_text), encoding="utf-8")
                    measurement_tool = self.runner.run(self.executable, ["-n", "-b", "-o", str(measurement_log), str(measurement_input)], working, timeout=self.timeout)
                    result["measurement_tool"] = measurement_tool.to_dict()
                    if measurement_tool.status != "success" or not measurement_log.is_file() or measurement_log.is_symlink() or not 0 < measurement_log.stat().st_size <= _MAX_BYTES:
                        raise ValueError("Ngspice measurement batch failed or produced no bounded log")
                    text = measurement_log.read_text(encoding="utf-8", errors="replace")
                    if re.search(r"(?im)^\s*(?:fatal|error\b|doAnalyses:)|(?:simulation|analysis).*aborted|failed to converge", text):
                        raise ValueError("Ngspice measurement log reports a failed analysis")
                    extra_artifacts.extend((measurement_log, measurement_input))
                for name in names:
                    match = re.search(rf"(?im)^\s*{re.escape(name)}\s*=\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?)\b", text)
                    if not match or not math.isfinite(float(match[1])):
                        raise ValueError(f"requested measurement has no finite result: {name}")
                    measurements[name] = float(match[1])
                if hashlib.sha256(source.read_bytes()).hexdigest() != result["input_sha256"]:
                    raise ValueError("SPICE input changed during simulation")
                paths = []
                for path in (waveform, log, *extra_artifacts):
                    target = destination / path.name
                    if target.is_symlink():
                        raise ValueError("refusing to overwrite simulation symlink")
                    target.write_bytes(path.read_bytes())
                    paths.append(str(target))
            except (OSError, ValueError, UnicodeError) as exc:
                return dict(result, reason=f"Invalid simulation evidence: {exc}")
        return dict(result, status="passed", reason="", measurements=measurements, waveform=metadata,
                    paths=paths, sha256={path: hashlib.sha256(Path(path).read_bytes()).hexdigest() for path in paths})
