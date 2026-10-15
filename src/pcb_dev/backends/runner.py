"""Bounded, shell-free external tool execution.

Only the named engineering tools are accepted.  No arbitrary command templates,
shell expansion, inherited stdin, or package installation are performed.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import math
import os
from pathlib import Path
import shutil
import signal
import subprocess
import threading
from typing import Sequence


@dataclass(frozen=True)
class ToolResult:
    command: tuple[str, ...]
    status: str
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""
    executed: bool = False
    truncated: bool = False

    def to_dict(self) -> dict:
        result = asdict(self)
        result["command"] = list(self.command)
        return result


class ToolRunner:
    """Run an allowlisted executable, retaining at most ``max_output_bytes`` per stream."""

    ALLOWED = frozenset({"kicad-cli", "kicad-cli.exe", "ngspice", "ngspice.exe", "java", "java.exe"})

    def __init__(self, max_output_bytes: int = 65536):
        if type(max_output_bytes) is not int or not 256 <= max_output_bytes <= 1048576:
            raise ValueError("max_output_bytes must be an integer from 256 to 1048576")
        self.max_output_bytes = max_output_bytes

    def run(self, executable: str, args: Sequence[str], cwd: Path, timeout: float = 120) -> ToolResult:
        if not isinstance(executable, str) or Path(executable).name not in self.ALLOWED:
            raise ValueError("executable is not an allowlisted engineering tool")
        if isinstance(args, (str, bytes)) or any(not isinstance(a, str) or "\0" in a for a in args):
            raise ValueError("arguments must be a sequence of strings without NUL")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 3600:
            raise ValueError("timeout must be finite and between 0 and 3600 seconds")
        workdir = Path(cwd).resolve(strict=True)
        if not workdir.is_dir():
            raise ValueError("cwd must be an existing directory")
        resolved = shutil.which(executable)
        command = (resolved or executable, *args)
        if resolved is None:
            return ToolResult(command, "unavailable", stderr=f"Executable unavailable: {executable}")
        buffers = [bytearray(), bytearray()]
        truncated = [False, False]

        def drain(stream, index):
            try:
                while chunk := stream.read(8192):
                    available = self.max_output_bytes - len(buffers[index])
                    buffers[index].extend(chunk[:available])
                    truncated[index] |= len(chunk) > available
            finally:
                stream.close()

        try:
            process = subprocess.Popen(command, cwd=workdir, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       shell=False, close_fds=True, start_new_session=os.name == "posix")
        except OSError as exc:
            return ToolResult(command, "unavailable", stderr=str(exc))
        readers = [threading.Thread(target=drain, args=(stream, i), daemon=True)
                   for i, stream in enumerate((process.stdout, process.stderr))]
        for reader in readers:
            reader.start()
        status = "failed"
        try:
            process.wait(timeout=timeout)
            status = "success" if process.returncode == 0 else "failed"
        except subprocess.TimeoutExpired:
            status = "timeout"
        finally:
            # Also close descendant-held pipes.  A child must not outlive the bounded call.
            if os.name == "posix":
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            elif process.poll() is None:
                process.kill()
            process.wait()
            for reader in readers:
                reader.join(timeout=2)
        return ToolResult(command, status, process.returncode,
                          buffers[0].decode("utf-8", errors="replace"),
                          buffers[1].decode("utf-8", errors="replace"), True, any(truncated))
