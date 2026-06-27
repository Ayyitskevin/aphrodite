from pathlib import Path

from fastapi.testclient import TestClient

from aphrodite.api import create_app
from aphrodite.config import Settings
from aphrodite.domain import JobCreate, JobOutputCreate, ProductInput, build_render_results
from aphrodite.store import JobStore


def job_request() -> JobCreate:
    return JobCreate(
        product=ProductInput(name="Mug", source_image_uri="file:///mug.jpg"),
        marketplace_targets=["catalog_square"],
    )


def test_build_render_results_projects_cost_and_spec(tmp_path: Path) -> None:
    store = JobStore(str(tmp_path / "db.sqlite"))
    store.initialize()
    created = store.create_job(job_request())
    claim = store.claim_next_job(worker_id="renderer")
    assert claim is not None
    store.complete_job_output(
        job_id=created.id,
        output=JobOutputCreate(
            claim_token=claim.claim_token,
            variant_id="catalog_square",
            storage_path="outputs/catalog_square.jpg",
            content_type="image/jpeg",
            bytes=1024,
            sha256="a" * 64,
            width=2000,
            height=2000,
            cost_usd=0.02,
            cost_ticks=200_000_000,
            model="grok-imagine-image-quality",
            latency_ms=900,
        ),
    )
    job = store.get_job(created.id)
    assert job is not None

    envelope = build_render_results(job)

    assert len(envelope.renders) == 1
    render = envelope.renders[0]
    assert render.kind == "catalog_square"
    assert render.output_path == "outputs/catalog_square.jpg"
    assert render.cost_usd == 0.02
    assert render.model == "grok-imagine-image-quality"
    assert isinstance(render.spec, dict)
    assert render.spec["width"] == 2000
    # Mise consumes a strict envelope where cost_usd is a number >= 0.
    payload = envelope.model_dump()
    assert payload["renders"][0]["cost_usd"] >= 0


def test_renders_endpoint_returns_envelope_and_404(tmp_path: Path) -> None:
    app = create_app(
        settings=Settings(
            db_path=str(tmp_path / "api.db"),
            media_root=str(tmp_path / "media"),
        )
    )
    test_client = TestClient(app)

    created = test_client.post(
        "/v1/jobs",
        json={
            "product": {"name": "Mug", "source_image_uri": "file:///mug.jpg"},
            "marketplace_targets": ["catalog_square"],
        },
    )
    assert created.status_code == 201
    job_id = created.json()["id"]

    response = test_client.get(f"/v1/jobs/{job_id}/renders")
    assert response.status_code == 200
    # No outputs rendered yet, so the envelope is empty but well-formed.
    assert response.json() == {"renders": []}

    missing = test_client.get("/v1/jobs/does-not-exist/renders")
    assert missing.status_code == 404
