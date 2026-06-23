"""Image intake helpers for Aphrodite source assets."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
JPEG_SIGNATURE = b"\xff\xd8"
SUPPORTED_CONTENT_TYPES = {
    "image/jpeg": "jpg",
    "image/png": "png",
}
GENERIC_CONTENT_TYPES = {"", "application/octet-stream"}


class AssetValidationError(Exception):
    def __init__(self, message: str, *, status_code: int = 415) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class ValidatedAsset:
    original_filename: str
    content_type: str
    extension: str
    bytes: int
    sha256: str
    width: int
    height: int
    content: bytes


def validate_image_upload(
    *,
    content: bytes,
    filename: str | None,
    declared_content_type: str | None,
    max_bytes: int,
) -> ValidatedAsset:
    if not content:
        raise AssetValidationError("uploaded asset is empty", status_code=422)
    if len(content) > max_bytes:
        raise AssetValidationError("uploaded asset exceeds the maximum size", status_code=413)

    content_type, extension, width, height = _sniff_image(content)
    declared = (declared_content_type or "").split(";")[0].strip().lower()
    if declared not in GENERIC_CONTENT_TYPES and declared != content_type:
        raise AssetValidationError("declared content type does not match image bytes")

    return ValidatedAsset(
        original_filename=_safe_filename(filename),
        content_type=content_type,
        extension=extension,
        bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        width=width,
        height=height,
        content=content,
    )


def storage_path_for(asset_id: str, extension: str) -> str:
    prefix = asset_id.replace("-", "")[:2]
    return f"originals/{prefix}/{asset_id}.{extension}"


def write_asset_file(*, media_root: str, relative_path: str, content: bytes) -> Path:
    target = Path(media_root) / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    return target


def _safe_filename(filename: str | None) -> str:
    raw = Path(filename or "upload").name.strip() or "upload"
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", raw)
    return safe[:180] or "upload"


def _sniff_image(content: bytes) -> tuple[str, str, int, int]:
    if content.startswith(PNG_SIGNATURE):
        return _parse_png(content)
    if content.startswith(JPEG_SIGNATURE):
        return _parse_jpeg(content)
    raise AssetValidationError("unsupported image type; upload a PNG or JPEG")


def _parse_png(content: bytes) -> tuple[str, str, int, int]:
    if len(content) < 24 or content[12:16] != b"IHDR":
        raise AssetValidationError("invalid PNG image", status_code=422)
    width = int.from_bytes(content[16:20], "big")
    height = int.from_bytes(content[20:24], "big")
    _validate_dimensions(width, height)
    return "image/png", "png", width, height


def _parse_jpeg(content: bytes) -> tuple[str, str, int, int]:
    index = 2
    end = len(content)
    start_of_frame_markers = {
        0xC0,
        0xC1,
        0xC2,
        0xC3,
        0xC5,
        0xC6,
        0xC7,
        0xC9,
        0xCA,
        0xCB,
        0xCD,
        0xCE,
        0xCF,
    }

    while index < end:
        while index < end and content[index] == 0xFF:
            index += 1
        if index >= end:
            break

        marker = content[index]
        index += 1
        if marker in {0x01, *range(0xD0, 0xD9)}:
            continue
        if index + 2 > end:
            break

        segment_length = int.from_bytes(content[index : index + 2], "big")
        if segment_length < 2 or index + segment_length > end:
            break
        if marker in start_of_frame_markers:
            if segment_length < 7:
                break
            height = int.from_bytes(content[index + 3 : index + 5], "big")
            width = int.from_bytes(content[index + 5 : index + 7], "big")
            _validate_dimensions(width, height)
            return "image/jpeg", "jpg", width, height
        index += segment_length

    raise AssetValidationError("invalid JPEG image", status_code=422)


def _validate_dimensions(width: int, height: int) -> None:
    if width <= 0 or height <= 0:
        raise AssetValidationError("image dimensions must be positive", status_code=422)
