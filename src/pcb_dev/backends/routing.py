"""Optional Freerouting DSN-to-SES adapter; never claims a verified PCB."""

from __future__ import annotations

import hashlib
from pathlib import Path
import re
import tempfile
import zipfile

from .runner import ToolRunner

_MAX_DSN_BYTES = 64 * 1024 * 1024


def _session_is_structured(text: str) -> bool:
    if not re.match(r"\s*\(session\s", text) or "(routes" not in text or "(network_out" not in text:
        return False
    # SES has ordinary quoted atoms. DSN's special `(string_quote ")` syntax
    # prevents using this small scanner as a complete DSN parser.
    quoted, escaped, depth = False, False, 0
    for char in text:
        if escaped:
            escaped = False
        elif quoted and char == "\\":
            escaped = True
        elif char == '"':
            quoted = not quoted
        elif not quoted:
            depth += (char == "(") - (char == ")")
            if depth < 0:
                return False
    return depth == 0 and not quoted


class FreeroutingBackend:
    """Run a caller-provided trusted Freerouting JAR (2.2+ headless CLI).

    This is executable Java code, not a downloaded or sandboxed model. An optional
    expected SHA256 pins the reviewed JAR. The produced SES must be imported into
    the real source board and checked there before routing can be accepted.
    """

    def __init__(self, jar: Path, runner: ToolRunner | None = None, executable: str = "java",
                 timeout: float = 600, expected_sha256: str | None = None):
        self.jar = Path(jar).resolve()
        self.runner = runner or ToolRunner()
        self.executable = executable
        self.timeout = timeout
        if expected_sha256 is not None and not re.fullmatch(r"[a-fA-F0-9]{64}", expected_sha256):
            raise ValueError("expected_sha256 must be a SHA256 hexadecimal digest")
        self.expected_sha256 = expected_sha256.lower() if expected_sha256 else None

    def diagnose(self) -> dict:
        result = {"status": "unavailable", "version": None, "jar_sha256": None}
        if not self.jar.is_file() or self.jar.suffix.lower() != ".jar" or not 0 < self.jar.stat().st_size <= 512 * 1024 * 1024:
            return dict(result, reason="A local trusted Freerouting JAR is required")
        result["jar_sha256"] = hashlib.sha256(self.jar.read_bytes()).hexdigest()
        if self.expected_sha256 and result["jar_sha256"] != self.expected_sha256:
            return dict(result, status="failed", reason="Freerouting JAR hash does not match the expected digest")
        try:
            with zipfile.ZipFile(self.jar) as archive:
                info = archive.getinfo("META-INF/MANIFEST.MF")
                if info.file_size > 65536:
                    raise ValueError("oversized JAR manifest")
                manifest = archive.read(info).decode("utf-8")
                version = re.search(r"(?im)^Implementation-Version:\s*(\S+)", manifest)
                result["version"] = version[1] if version else None
        except (OSError, ValueError, KeyError, zipfile.BadZipFile) as exc:
            return dict(result, status="failed", reason=f"Invalid Freerouting JAR: {exc}")
        with tempfile.TemporaryDirectory(prefix="pcb-dev-java-version-") as directory:
            tool = self.runner.run(self.executable, ["-version"], Path(directory), timeout=min(self.timeout, 15))
        java_match = re.search(r'version\s+"([^"\n]+)"', tool.stdout + tool.stderr)
        return dict(result, status="passed" if tool.status == "success" and java_match else ("unavailable" if tool.status == "unavailable" else "failed"),
                    java_version=java_match[1] if java_match else None, tool=tool.to_dict(), reason="" if tool.status == "success" and java_match else "A working Java runtime is required")

    def route(self, dsn: Path, output_dir: Path, max_passes: int = 100, threads: int = 1) -> dict:
        if type(max_passes) is not int or not 1 <= max_passes <= 10000 or type(threads) is not int or not 1 <= threads <= 64:
            raise ValueError("max_passes must be 1..10000 and threads must be 1..64")
        source = Path(dsn).resolve()
        result = {"stage": "routing", "status": "failed", "executed": False,
                  "routing_verified": False, "board_updated": False, "fabrication_ready": False, "paths": []}
        if not source.is_file() or not 0 < source.stat().st_size <= _MAX_DSN_BYTES:
            return dict(result, reason="Specctra DSN input is missing, empty, or oversized")
        raw = source.read_bytes()
        try:
            text = raw.decode("utf-8")
        except UnicodeError:
            return dict(result, reason="DSN input must be UTF-8")
        if not re.match(r"\s*\(pcb\s", text) or "\0" in text:
            return dict(result, reason="Input is not a Specctra PCB description")
        result["input_sha256"] = hashlib.sha256(raw).hexdigest()
        diagnostic = self.diagnose()
        result.update(jar_sha256=diagnostic["jar_sha256"], tool_version=diagnostic["version"], java_version=diagnostic.get("java_version"))
        if diagnostic["status"] != "passed":
            return dict(result, status=diagnostic["status"], reason=diagnostic["reason"])
        destination = Path(output_dir).resolve()
        destination.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".freerouting-", dir=destination) as directory:
            working = Path(directory)
            # No adjacent .rules or user data is picked up from the source tree.
            copied = working / "design.dsn"
            copied.write_bytes(raw)
            session = working / "routed.ses"
            args = ["-Djava.awt.headless=true", "-jar", str(self.jar), "-de", str(copied), "-do", str(session), "-da", "-mp", str(max_passes), "-mt", str(threads), "--gui.enabled=false", "--api_server.enabled=false", "--logging.file.enabled=false", "--user_data_path=" + str(working / "state")]
            tool = self.runner.run(self.executable, args, working, timeout=self.timeout)
            result.update(executed=tool.executed, returncode=tool.returncode, tool=tool.to_dict())
            if tool.status != "success":
                return dict(result, status="unavailable" if tool.status == "unavailable" else "failed", reason="Freerouting did not complete successfully")
            try:
                if not session.is_file() or session.is_symlink() or not 0 < session.stat().st_size <= _MAX_DSN_BYTES:
                    raise ValueError("Freerouting produced no bounded SES artifact")
                session_text = session.read_text(encoding="utf-8")
                if not _session_is_structured(session_text):
                    raise ValueError("Freerouting output is not a complete session with route data")
                if hashlib.sha256(source.read_bytes()).hexdigest() != result["input_sha256"] or hashlib.sha256(self.jar.read_bytes()).hexdigest() != result["jar_sha256"]:
                    raise ValueError("DSN or Freerouting JAR changed during routing")
                target = destination / "routed.ses"
                if target.is_symlink():
                    raise ValueError("refusing to overwrite session symlink")
                target.write_text(session_text, encoding="utf-8")
            except (OSError, ValueError, UnicodeError) as exc:
                return dict(result, reason=f"Invalid routing evidence: {exc}")
        return dict(result, status="passed", reason="SES generated; import and native board DRC are required", paths=[str(target)],
                    sha256={str(target): hashlib.sha256(target.read_bytes()).hexdigest()})
