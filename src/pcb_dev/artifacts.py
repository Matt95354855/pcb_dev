"""Deterministic artifact manifests and archives with traversal-safe verification."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import zipfile
from pathlib import Path, PurePosixPath


class ArtifactError(ValueError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def json_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def write_manifest(directory: Path, *, design_hash: str, kind: str = "preview") -> dict:
    directory = directory.resolve()
    files = {}
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise ArtifactError(f"Symlinks are not allowed in build artifacts: {path.name}")
        if path.is_file() and path != directory / "manifest.json":
            files[path.relative_to(directory).as_posix()] = {
                "sha256": sha256_file(path), "size": path.stat().st_size,
            }
    manifest = {"schema_version": 1, "kind": kind, "design_sha256": design_hash,
                "files": files}
    atomic_write(directory / "manifest.json", json_text(manifest))
    return manifest


def verify_manifest(directory: str | Path) -> dict:
    directory = Path(directory).resolve()
    try:
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ArtifactError(f"Cannot read manifest: {exc}") from exc
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise ArtifactError("Unsupported artifact manifest")
    if not isinstance(manifest.get("files"), dict) or not manifest["files"]:
        raise ArtifactError("Manifest contains no files")
    expected = set()
    for name, record in manifest["files"].items():
        pure = PurePosixPath(name)
        if (not isinstance(name, str) or not name or "\\" in name or pure.is_absolute()
                or any(part in {"..", "."} for part in name.split("/"))
                or ":" in name or name == "manifest.json"):
            raise ArtifactError(f"Unsafe artifact path: {name!r}")
        path = directory.joinpath(*pure.parts)
        if any(parent.is_symlink() for parent in [path, *path.parents] if parent != directory):
            raise ArtifactError(f"Symlink artifact: {name}")
        if not path.is_relative_to(directory) or not path.is_file():
            raise ArtifactError(f"Missing artifact: {name}")
        if not isinstance(record, dict) or not isinstance(record.get("sha256"), str):
            raise ArtifactError(f"Invalid record for {name}")
        if record.get("size") != path.stat().st_size or record["sha256"] != sha256_file(path):
            raise ArtifactError(f"Artifact changed: {name}")
        expected.add(name)
    actual = set()
    for path in directory.rglob("*"):
        if path.is_symlink():
            raise ArtifactError(f"Symlink artifact: {path.name}")
        if path.is_file() and path != directory / "manifest.json":
            actual.add(path.relative_to(directory).as_posix())
    if actual != expected:
        raise ArtifactError(f"Untracked artifacts: {sorted(actual - expected)}")
    return manifest


def archive_build(directory: str | Path, output: str | Path) -> Path:
    """Archive only verified files; preview remains explicitly labeled preview."""
    directory, output = Path(directory).resolve(), Path(output).resolve()
    if output.is_relative_to(directory):
        raise ArtifactError("Archive output must be outside the artifact directory")
    verify_manifest(directory)
    if output.exists():
        raise ArtifactError(f"Archive already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".pcb-archive-", dir=output.parent)
    os.close(descriptor)
    temporary = Path(name)
    try:
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for path in sorted(p for p in directory.rglob("*") if p.is_file()):
                info = zipfile.ZipInfo(path.relative_to(directory).as_posix(), (1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = 3
                info.external_attr = (stat.S_IFREG | 0o644) << 16
                archive.writestr(info, path.read_bytes())
        # Exclusive creation prevents concurrent calls from silently replacing an archive.
        with output.open("xb") as stream:
            stream.write(temporary.read_bytes())
    finally:
        temporary.unlink(missing_ok=True)
    return output
