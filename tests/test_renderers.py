from pathlib import Path

from aphrodite.domain import JobRecord, JobStatus, OutputVariant, ProductInput
from aphrodite.renderers import LocalStubRendererBackend, get_renderer_backend


def job() -> JobRecord:
    return JobRecord(
        id="job-123",
        status=JobStatus.RENDERING,
        product=ProductInput(name="Matte mug", source_image_uri="file:///mug.jpg"),
        marketplace_targets=["catalog_square"],
        output_plan=[],
        priority=5,
        created_at="2026-06-23T00:00:00Z",
        updated_at="2026-06-23T00:00:00Z",
    )


def variant(output_format: str = "jpg", variant_id: str = "catalog_square") -> OutputVariant:
    return OutputVariant(
        id=variant_id,
        target_id="catalog_square",
        label="Catalog square",
        width=2000,
        height=2000,
        aspect_ratio="1:1",
        output_format=output_format,
        background="clean_white",
        safe_margin_percent=8,
    )


def test_local_stub_renderer_writes_deterministic_file(tmp_path: Path) -> None:
    backend = LocalStubRendererBackend(media_root=str(tmp_path / "media"))

    first = backend.render(job=job(), variant=variant())
    second = backend.render(job=job(), variant=variant())
    output_path = tmp_path / "media" / first.storage_path

    assert first == second
    assert first.storage_path == "outputs/job-123/catalog_square.jpg"
    assert first.content_type == "image/jpeg"
    assert first.width == 2000
    assert first.bytes == output_path.stat().st_size
    assert len(first.sha256) == 64
    assert output_path.read_bytes().startswith(b"APHRODITE_LOCAL_STUB_OUTPUT")


def test_local_stub_renderer_maps_png_outputs(tmp_path: Path) -> None:
    rendered = LocalStubRendererBackend(media_root=str(tmp_path / "media")).render(
        job=job(),
        variant=variant("png"),
    )

    assert rendered.storage_path.endswith(".png")
    assert rendered.content_type == "image/png"
    assert (tmp_path / "media" / rendered.storage_path).exists()


def test_local_stub_renderer_sanitizes_variant_paths(tmp_path: Path) -> None:
    rendered = LocalStubRendererBackend(media_root=str(tmp_path / "media")).render(
        job=job(),
        variant=variant("jpg", variant_id="../hero banner"),
    )

    assert rendered.storage_path == "outputs/job-123/hero_banner.jpg"
    assert (tmp_path / "media" / rendered.storage_path).exists()


def test_get_renderer_backend_returns_local_stub(tmp_path: Path) -> None:
    backend = get_renderer_backend("local_stub", media_root=str(tmp_path / "media"))

    assert backend.name == "local_stub"
