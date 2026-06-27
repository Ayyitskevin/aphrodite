"""Renderer backend contracts for Aphrodite workers."""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from typing import Protocol

from PIL import Image, ImageDraw

from aphrodite.domain import JobRecord, OutputVariant
from aphrodite.storage import output_relative_path, write_output_file


class RendererError(Exception):
    """Raised when a renderer backend cannot produce an output."""


@dataclass(frozen=True, slots=True)
class RenderedOutput:
    variant_id: str
    storage_path: str
    content_type: str
    bytes: int
    sha256: str
    width: int
    height: int
    # Real per-render spend + provenance the worker forwards to the API so Mise
    # can sum cost_usd against its hard cap. Defaulted so a backend that does not
    # cost anything (local_stub) or predates the contract still constructs.
    cost_usd: float = 0.0
    cost_ticks: int | None = None
    model: str = "unknown"
    latency_ms: int | None = None

    def as_worker_payload(self, *, claim_token: str) -> dict[str, str | int | float | None]:
        return {
            "claim_token": claim_token,
            "variant_id": self.variant_id,
            "storage_path": self.storage_path,
            "content_type": self.content_type,
            "bytes": self.bytes,
            "sha256": self.sha256,
            "width": self.width,
            "height": self.height,
            "cost_usd": self.cost_usd,
            "cost_ticks": self.cost_ticks,
            "model": self.model,
            "latency_ms": self.latency_ms,
        }


class RendererBackend(Protocol):
    name: str

    def render(self, *, job: JobRecord, variant: OutputVariant) -> RenderedOutput:
        """Render one planned variant for a claimed job."""


class LocalStubRendererBackend:
    name = "local_stub"

    def __init__(self, *, media_root: str = "media") -> None:
        self.media_root = media_root

    def render(self, *, job: JobRecord, variant: OutputVariant) -> RenderedOutput:
        extension = _extension_for(variant.output_format)
        content_type = _content_type_for(extension)
        storage_path = output_relative_path(
            job_id=job.id,
            variant_id=variant.id,
            extension=extension,
        )
        stored = write_output_file(
            media_root=self.media_root,
            relative_path=storage_path,
            content=_stub_image(job=job, variant=variant, content_type=content_type),
        )

        return RenderedOutput(
            variant_id=variant.id,
            storage_path=stored.relative_path,
            content_type=content_type,
            bytes=stored.bytes,
            sha256=stored.sha256,
            width=variant.width,
            height=variant.height,
            # The stub spends no money and does no remote call; report the zero
            # cost and a fixed 0ms latency explicitly so they are recorded facts
            # rather than missing fields, and so stub output stays deterministic.
            cost_usd=0.0,
            cost_ticks=0,
            model=self.name,
            latency_ms=0,
        )


def get_renderer_backend(name: str, *, media_root: str = "media") -> RendererBackend:
    normalized = name.strip().lower()
    if normalized == "local_stub":
        return LocalStubRendererBackend(media_root=media_root)
    if normalized == "xai":
        from aphrodite.xai import XAIImageRendererBackend

        return XAIImageRendererBackend.from_env(media_root=media_root)
    raise RendererError(f"unknown renderer backend: {name}")


def _stub_image(*, job: JobRecord, variant: OutputVariant, content_type: str) -> bytes:
    transparent = content_type == "image/png" and variant.background == "transparent"
    mode = "RGBA" if transparent else "RGB"
    background = (255, 255, 255, 0) if transparent else _background_color(variant.background)
    image = Image.new(mode, (variant.width, variant.height), background)
    draw = ImageDraw.Draw(image)

    margin_x = max(12, int(variant.width * 0.18))
    margin_y = max(12, int(variant.height * 0.18))
    box = (
        margin_x,
        margin_y,
        max(margin_x + 1, variant.width - margin_x),
        max(margin_y + 1, variant.height - margin_y),
    )
    fill = (32, 37, 41, 255) if transparent else (32, 37, 41)
    outline = (15, 118, 110, 255) if transparent else (15, 118, 110)
    draw.rounded_rectangle(box, radius=max(8, min(variant.width, variant.height) // 18), fill=fill)
    draw.rounded_rectangle(
        (
            box[0] + max(4, variant.width // 80),
            box[1] + max(4, variant.height // 80),
            box[2] - max(4, variant.width // 80),
            box[3] - max(4, variant.height // 80),
        ),
        radius=max(6, min(variant.width, variant.height) // 22),
        outline=outline,
        width=max(2, min(variant.width, variant.height) // 120),
    )

    if content_type == "image/jpeg":
        image = image.convert("RGB")
        return _encode_image(image, image_format="JPEG", quality=88)
    if content_type == "image/png":
        return _encode_image(image, image_format="PNG")
    return (
        "\n".join(
            [
                "APHRODITE_LOCAL_STUB_OUTPUT",
                f"job={job.id}",
                f"variant={variant.id}",
                f"product={job.product.name}",
                "",
            ]
        )
    ).encode("utf-8")


def _background_color(background: str) -> tuple[int, int, int]:
    if background == "clean_white":
        return (248, 250, 252)
    if background == "studio_shadow":
        return (231, 235, 239)
    if background == "brand_gradient":
        return (226, 245, 241)
    if background == "lifestyle":
        return (237, 232, 224)
    return (245, 247, 250)


def _encode_image(image: Image.Image, *, image_format: str, quality: int | None = None) -> bytes:
    with BytesIO() as buffer:
        kwargs = {"quality": quality} if quality is not None else {}
        image.save(buffer, format=image_format, **kwargs)
        return buffer.getvalue()


def _extension_for(output_format: str) -> str:
    normalized = output_format.strip().lower()
    if normalized in {"jpg", "jpeg"}:
        return "jpg"
    if normalized == "png":
        return "png"
    return normalized or "bin"


def _content_type_for(extension: str) -> str:
    if extension == "jpg":
        return "image/jpeg"
    if extension == "png":
        return "image/png"
    return "application/octet-stream"
