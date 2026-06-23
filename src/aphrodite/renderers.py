"""Renderer backend contracts for Aphrodite workers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

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

    def as_worker_payload(self, *, claim_token: str) -> dict[str, str | int]:
        return {
            "claim_token": claim_token,
            "variant_id": self.variant_id,
            "storage_path": self.storage_path,
            "content_type": self.content_type,
            "bytes": self.bytes,
            "sha256": self.sha256,
            "width": self.width,
            "height": self.height,
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
            content=_stub_content(job=job, variant=variant),
        )

        return RenderedOutput(
            variant_id=variant.id,
            storage_path=stored.relative_path,
            content_type=content_type,
            bytes=stored.bytes,
            sha256=stored.sha256,
            width=variant.width,
            height=variant.height,
        )


def get_renderer_backend(name: str, *, media_root: str = "media") -> RendererBackend:
    normalized = name.strip().lower()
    if normalized == "local_stub":
        return LocalStubRendererBackend(media_root=media_root)
    raise RendererError(f"unknown renderer backend: {name}")


def _stub_content(*, job: JobRecord, variant: OutputVariant) -> bytes:
    return (
        "\n".join(
            [
                "APHRODITE_LOCAL_STUB_OUTPUT",
                f"job={job.id}",
                f"variant={variant.id}",
                f"product={job.product.name}",
                f"size={variant.width}x{variant.height}",
                f"background={variant.background}",
                f"format={variant.output_format}",
                "",
            ]
        )
    ).encode("utf-8")


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
