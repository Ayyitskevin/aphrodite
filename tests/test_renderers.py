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


def variant(output_format: str = "jpg") -> OutputVariant:
    return OutputVariant(
        id="catalog_square",
        target_id="catalog_square",
        label="Catalog square",
        width=2000,
        height=2000,
        aspect_ratio="1:1",
        output_format=output_format,
        background="clean_white",
        safe_margin_percent=8,
    )


def test_local_stub_renderer_is_deterministic() -> None:
    backend = LocalStubRendererBackend()

    first = backend.render(job=job(), variant=variant())
    second = backend.render(job=job(), variant=variant())

    assert first == second
    assert first.storage_path == "outputs/job-123/catalog_square.jpg"
    assert first.content_type == "image/jpeg"
    assert first.width == 2000
    assert len(first.sha256) == 64


def test_local_stub_renderer_maps_png_outputs() -> None:
    rendered = LocalStubRendererBackend().render(job=job(), variant=variant("png"))

    assert rendered.storage_path.endswith(".png")
    assert rendered.content_type == "image/png"


def test_get_renderer_backend_returns_local_stub() -> None:
    assert get_renderer_backend("local_stub").name == "local_stub"
