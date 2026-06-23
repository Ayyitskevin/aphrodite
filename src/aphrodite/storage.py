"""Safe local storage helpers for renderer outputs."""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass
from pathlib import Path


class OutputStorageError(Exception):
    """Raised when an output path cannot be safely written."""


@dataclass(frozen=True, slots=True)
class StoredFile:
    relative_path: str
    absolute_path: Path
    bytes: int
    sha256: str


def output_relative_path(*, job_id: str, variant_id: str, extension: str) -> str:
    job_segment = safe_path_segment(job_id, fallback="job")
    variant_segment = safe_path_segment(variant_id, fallback="variant")
    extension_segment = safe_extension(extension)
    return f"outputs/{job_segment}/{variant_segment}.{extension_segment}"


def write_output_file(*, media_root: str, relative_path: str, content: bytes) -> StoredFile:
    target = resolve_media_file_path(media_root=media_root, relative_path=relative_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        temp.write_bytes(content)
        temp.replace(target)
    except OSError as exc:
        temp.unlink(missing_ok=True)
        raise OutputStorageError("failed to write output file") from exc
    return StoredFile(
        relative_path=target.relative_to(Path(media_root).resolve()).as_posix(),
        absolute_path=target,
        bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
    )


def resolve_media_file_path(*, media_root: str, relative_path: str) -> Path:
    root = Path(media_root).resolve()
    target = (root / relative_path).resolve()
    if not target.is_relative_to(root):
        raise OutputStorageError("media path escapes media root")
    return target


def resolve_existing_media_file(*, media_root: str, relative_path: str) -> Path:
    target = resolve_media_file_path(media_root=media_root, relative_path=relative_path)
    if not target.is_file():
        raise FileNotFoundError(relative_path)
    return target


def safe_path_segment(value: str, *, fallback: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", value)
    while ".." in safe:
        safe = safe.replace("..", ".")
    safe = safe.strip("._-")
    if not safe:
        return fallback
    return safe[:160]


def safe_extension(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9]+", "", value.lower())
    return safe[:16] or "bin"
