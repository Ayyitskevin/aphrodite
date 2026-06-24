"""Image intake helpers for Aphrodite source assets."""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from PIL import Image, UnidentifiedImageError

SUPPORTED_IMAGE_FORMATS = {
    "JPEG": ("image/jpeg", "jpg"),
    "PNG": ("image/png", "png"),
}
GENERIC_CONTENT_TYPES = {"", "application/octet-stream"}


class AssetValidationError(Exception):
    def __init__(self, message: str, *, status_code: int = 415) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class AssetStorageError(Exception):
    """Raised when an uploaded source asset cannot be safely stored."""


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
    max_pixels: int = 50_000_000,
) -> ValidatedAsset:
    if not content:
        raise AssetValidationError("uploaded asset is empty", status_code=422)
    if len(content) > max_bytes:
        raise AssetValidationError("uploaded asset exceeds the maximum size", status_code=413)

    declared = (declared_content_type or "").split(";")[0].strip().lower()
    supported_content_types = {
        content_type for content_type, _extension in SUPPORTED_IMAGE_FORMATS.values()
    }
    if declared not in GENERIC_CONTENT_TYPES and declared not in supported_content_types:
        raise AssetValidationError("unsupported image type; upload a PNG or JPEG")

    content_type, extension, width, height = _decode_image(content, max_pixels=max_pixels)
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
    root = Path(media_root).resolve()
    target = (root / relative_path).resolve()
    if not target.is_relative_to(root):
        raise AssetStorageError("asset path escapes media root")

    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        temp.write_bytes(content)
        temp.replace(target)
    except OSError as exc:
        temp.unlink(missing_ok=True)
        raise AssetStorageError("failed to write asset file") from exc
    return target


def _safe_filename(filename: str | None) -> str:
    raw = Path(filename or "upload").name.strip() or "upload"
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", raw).strip("._-")
    return safe[:180] or "upload"


def _decode_image(content: bytes, *, max_pixels: int) -> tuple[str, str, int, int]:
    try:
        with Image.open(BytesIO(content)) as image:
            image.verify()
        with Image.open(BytesIO(content)) as image:
            image_format = image.format or ""
            width, height = image.size
    except (SyntaxError, UnidentifiedImageError, OSError, ValueError) as exc:
        raise AssetValidationError("invalid or unsupported image", status_code=422) from exc

    supported = SUPPORTED_IMAGE_FORMATS.get(image_format.upper())
    if supported is None:
        raise AssetValidationError("unsupported image type; upload a PNG or JPEG")

    _validate_dimensions(width, height, max_pixels=max_pixels)
    content_type, extension = supported
    return content_type, extension, width, height


def _validate_dimensions(width: int, height: int, *, max_pixels: int) -> None:
    if width <= 0 or height <= 0:
        raise AssetValidationError("image dimensions must be positive", status_code=422)
    # Read from the header before anything decodes the pixels, so a bomb is rejected
    # here rather than when the renderer later expands it into memory.
    if width * height > max_pixels:
        raise AssetValidationError(
            "image resolution exceeds the maximum allowed pixels", status_code=413
        )
