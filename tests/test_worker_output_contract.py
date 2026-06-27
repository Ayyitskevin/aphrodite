"""Pins the Mise worker output contract: the renders envelope and cost_usd >= 0,
plus the CI guarantee that the default render path makes no live generation call.
"""

import urllib.request
from pathlib import Path

import pytest
from pydantic import ValidationError

from aphrodite.domain import (
    JobCreate,
    JobOutputCreate,
    JobRecord,
    JobStatus,
    OutputVariant,
    ProductInput,
    RenderResult,
    build_render_results,
)
from aphrodite.renderers import LocalStubRendererBackend
from aphrodite.store import JobStore
from aphrodite.worker import WorkerConfig

CONTRACT_KEYS = {"source_asset_id", "kind", "spec", "output_path", "cost_usd"}


def _completed_job(tmp_path: Path) -> JobRecord:
    store = JobStore(str(tmp_path / "db.sqlite"))
    store.initialize()
    created = store.create_job(
        JobCreate(
            product=ProductInput(name="Mug", source_image_uri="file:///mug.jpg"),
            marketplace_targets=["catalog_square"],
        )
    )
    claim = store.claim_next_job(worker_id="renderer")
    assert claim is not None
    store.complete_job_output(
        job_id=created.id,
        output=JobOutputCreate(
            claim_token=claim.claim_token,
            variant_id="catalog_square",
            storage_path="outputs/catalog_square.jpg",
            content_type="image/jpeg",
            bytes=10,
            sha256="a" * 64,
            width=2000,
            height=2000,
            cost_usd=0.05,
            model="grok-imagine-image-quality",
        ),
    )
    job = store.get_job(created.id)
    assert job is not None
    return job


def test_renders_envelope_matches_contract_shape(tmp_path: Path) -> None:
    payload = build_render_results(_completed_job(tmp_path)).model_dump()

    assert set(payload) == {"renders"}
    assert len(payload["renders"]) == 1
    render = payload["renders"][0]

    # Every contract key is present.
    assert CONTRACT_KEYS <= set(render)
    # cost_usd is a number >= 0 (Mise sums it against its hard cap).
    assert isinstance(render["cost_usd"], (int, float)) and render["cost_usd"] >= 0
    # output_path is a string or null.
    assert render["output_path"] is None or isinstance(render["output_path"], str)
    # spec is a dict or a string.
    assert isinstance(render["spec"], (dict, str))


def test_render_result_rejects_negative_cost() -> None:
    with pytest.raises(ValidationError):
        RenderResult(kind="catalog_square", spec="catalog_square", cost_usd=-0.01)


def test_job_output_create_rejects_negative_cost() -> None:
    with pytest.raises(ValidationError):
        JobOutputCreate(
            claim_token="token",
            variant_id="catalog_square",
            storage_path="outputs/catalog_square.jpg",
            content_type="image/jpeg",
            bytes=10,
            sha256="a" * 64,
            width=2000,
            height=2000,
            cost_usd=-1.0,
        )


def test_default_worker_backend_is_local_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    # The default render path must never reach a paid generation backend, so CI
    # and fresh installs do no live generation.
    monkeypatch.delenv("APHRODITE_WORKER_BACKEND", raising=False)
    assert WorkerConfig.from_env().backend == "local_stub"


def test_local_stub_render_makes_no_network_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("the default render path must make no network call")

    monkeypatch.setattr(urllib.request, "urlopen", boom)

    job = JobRecord(
        id="job-1",
        status=JobStatus.RENDERING,
        product=ProductInput(name="Mug", source_image_uri="file:///mug.jpg"),
        marketplace_targets=["catalog_square"],
        output_plan=[],
        priority=5,
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
    )
    variant = OutputVariant(
        id="catalog_square",
        target_id="catalog_square",
        label="Catalog",
        width=64,
        height=64,
        aspect_ratio="1:1",
        output_format="jpg",
        background="clean_white",
        safe_margin_percent=8,
    )

    rendered = LocalStubRendererBackend(media_root=str(tmp_path / "media")).render(
        job=job, variant=variant
    )
    assert rendered.cost_usd == 0.0
    assert rendered.model == "local_stub"
