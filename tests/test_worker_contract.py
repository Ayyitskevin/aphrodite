import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError

from aphrodite.domain import (
    JobCreate,
    JobFailureCategory,
    JobOutputCreate,
    JobStatus,
    OutputReviewStatus,
    ProductInput,
)
from aphrodite.store import JobStore, OutputVariantNotFoundError


def job_request(*, priority: int = 5, quantity: int = 1) -> JobCreate:
    return JobCreate(
        product=ProductInput(
            name="Renderer mug",
            source_image_uri="file:///media/mug/source.jpg",
        ),
        marketplace_targets=["catalog_square"],
        quantity_per_target=quantity,
        priority=priority,
    )


def output_payload(claim_token: str, *, variant_id: str = "catalog_square") -> JobOutputCreate:
    return JobOutputCreate(
        claim_token=claim_token,
        variant_id=variant_id,
        storage_path=f"outputs/{variant_id}.jpg",
        content_type="image/jpeg",
        bytes=1024,
        sha256="a" * 64,
        width=2000,
        height=2000,
    )


def expire_claim(db_path: Path, job_id: str) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE jobs SET claim_expires_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00Z", job_id),
        )


def test_claim_next_job_is_exclusive_and_priority_ordered(tmp_path: Path) -> None:
    store = JobStore(str(tmp_path / "aphrodite.db"))
    store.initialize()
    low = store.create_job(job_request(priority=1))
    high = store.create_job(job_request(priority=9))

    first = store.claim_next_job(worker_id="worker-a")
    second = store.claim_next_job(worker_id="worker-b")
    third = store.claim_next_job(worker_id="worker-c")

    assert first is not None
    assert first.job.id == high.id
    assert first.job.status == JobStatus.RENDERING
    assert first.job.claimed_by == "worker-a"
    assert second is not None
    assert second.job.id == low.id
    assert third is None


def test_stale_claim_can_be_recovered(tmp_path: Path) -> None:
    db_path = tmp_path / "aphrodite.db"
    store = JobStore(str(db_path))
    store.initialize()
    job = store.create_job(job_request())
    first = store.claim_next_job(worker_id="worker-a")
    assert first is not None

    expire_claim(db_path, job.id)
    recovered = store.claim_next_job(worker_id="worker-b")

    assert recovered is not None
    assert recovered.job.id == job.id
    assert recovered.job.claimed_by == "worker-b"
    assert recovered.claim_token != first.claim_token


def test_claim_heartbeat_extends_active_claim(tmp_path: Path) -> None:
    store = JobStore(str(tmp_path / "aphrodite.db"))
    store.initialize()
    store.create_job(job_request())
    claim = store.claim_next_job(worker_id="worker-a")
    assert claim is not None

    refreshed = store.refresh_claim(
        job_id=claim.job.id,
        claim_token=claim.claim_token,
        claim_ttl_seconds=600,
    )
    wrong_token = store.refresh_claim(
        job_id=claim.job.id,
        claim_token="wrong",
    )

    assert refreshed is not None
    assert refreshed.claim_token == claim.claim_token
    assert refreshed.claim_expires_at >= claim.claim_expires_at
    assert wrong_token is None


def test_output_review_resets_when_output_is_replaced(tmp_path: Path) -> None:
    store = JobStore(str(tmp_path / "aphrodite.db"))
    store.initialize()
    store.create_job(job_request(quantity=2))
    claim = store.claim_next_job(worker_id="renderer")
    assert claim is not None

    first = store.complete_job_output(
        job_id=claim.job.id,
        output=output_payload(claim.claim_token, variant_id="catalog_square-1"),
    )
    assert first is not None
    approved = store.review_output(
        job_id=claim.job.id,
        variant_id="catalog_square-1",
        review_status=OutputReviewStatus.APPROVED,
    )
    assert approved is not None
    assert approved.review_status == OutputReviewStatus.APPROVED

    rerendered = store.complete_job_output(
        job_id=claim.job.id,
        output=output_payload(claim.claim_token, variant_id="catalog_square-1"),
    )

    assert rerendered is not None
    assert rerendered.review_status == OutputReviewStatus.PENDING_REVIEW
    assert rerendered.review_note is None
    assert rerendered.reviewed_at is None


def test_completing_all_outputs_marks_job_completed(tmp_path: Path) -> None:
    store = JobStore(str(tmp_path / "aphrodite.db"))
    store.initialize()
    store.create_job(job_request(quantity=2))
    claim = store.claim_next_job(worker_id="renderer")
    assert claim is not None

    first = store.complete_job_output(
        job_id=claim.job.id,
        output=output_payload(claim.claim_token, variant_id="catalog_square-1"),
    )
    midway = store.get_job(claim.job.id)
    second = store.complete_job_output(
        job_id=claim.job.id,
        output=output_payload(claim.claim_token, variant_id="catalog_square-2"),
    )
    completed = store.get_job(claim.job.id)

    assert first is not None
    assert second is not None
    assert midway is not None
    assert midway.status == JobStatus.RENDERING
    assert completed is not None
    assert completed.status == JobStatus.COMPLETED
    assert completed.claimed_by is None
    assert [output.variant_id for output in completed.outputs] == [
        "catalog_square-1",
        "catalog_square-2",
    ]


def test_output_requires_active_claim_and_known_variant(tmp_path: Path) -> None:
    store = JobStore(str(tmp_path / "aphrodite.db"))
    store.initialize()
    job = store.create_job(job_request())
    claim = store.claim_next_job(worker_id="renderer")
    assert claim is not None

    assert store.complete_job_output(
        job_id=job.id,
        output=output_payload("wrong"),
    ) is None

    try:
        store.complete_job_output(
            job_id=job.id,
            output=output_payload(claim.claim_token, variant_id="missing"),
        )
    except OutputVariantNotFoundError as exc:
        assert exc.variant_id == "missing"
    else:
        raise AssertionError("unknown output variant was accepted")


def test_fail_claimed_job_marks_job_failed(tmp_path: Path) -> None:
    store = JobStore(str(tmp_path / "aphrodite.db"))
    store.initialize()
    store.create_job(job_request())
    claim = store.claim_next_job(worker_id="renderer")
    assert claim is not None

    failed = store.fail_claimed_job(
        job_id=claim.job.id,
        claim_token=claim.claim_token,
        error="renderer crashed",
    )

    assert failed is not None
    assert failed.status == JobStatus.FAILED
    assert failed.error == "renderer crashed"
    assert failed.failure_category == JobFailureCategory.RENDERER_ERROR
    assert failed.claimed_by is None


def test_complete_output_persists_cost_and_provenance(tmp_path: Path) -> None:
    store = JobStore(str(tmp_path / "aphrodite.db"))
    store.initialize()
    store.create_job(job_request())
    claim = store.claim_next_job(worker_id="renderer")
    assert claim is not None

    persisted = store.complete_job_output(
        job_id=claim.job.id,
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
            latency_ms=1234,
        ),
    )

    assert persisted is not None
    assert persisted.cost_usd == 0.02
    assert persisted.cost_ticks == 200_000_000
    assert persisted.model == "grok-imagine-image-quality"
    assert persisted.latency_ms == 1234

    # Cost + provenance round-trip through a full job read.
    job = store.get_job(claim.job.id)
    assert job is not None
    assert job.outputs[0].cost_usd == 0.02
    assert job.outputs[0].model == "grok-imagine-image-quality"


def test_output_payload_defaults_cost_for_legacy_workers() -> None:
    # A worker that predates the cost contract omits the cost fields entirely;
    # the payload must still validate with a safe zero default so older workers
    # remain compatible.
    output = JobOutputCreate(
        claim_token="token",
        variant_id="catalog_square",
        storage_path="outputs/catalog_square.jpg",
        content_type="image/jpeg",
        bytes=1024,
        sha256="a" * 64,
        width=2000,
        height=2000,
    )

    assert output.cost_usd == 0.0
    assert output.cost_ticks is None
    assert output.model is None
    assert output.latency_ms is None


def test_negative_cost_is_rejected() -> None:
    with pytest.raises(ValidationError):
        JobOutputCreate(
            claim_token="token",
            variant_id="catalog_square",
            storage_path="outputs/catalog_square.jpg",
            content_type="image/jpeg",
            bytes=1024,
            sha256="a" * 64,
            width=2000,
            height=2000,
            cost_usd=-0.01,
        )


def test_migration_adds_cost_columns_to_legacy_outputs(tmp_path: Path) -> None:
    db_path = tmp_path / "aphrodite.db"
    # Build a pre-cost-contract job_outputs table that lacks the cost columns.
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE job_outputs (
              id TEXT PRIMARY KEY,
              job_id TEXT NOT NULL,
              variant_id TEXT NOT NULL,
              status TEXT NOT NULL,
              storage_path TEXT NOT NULL,
              content_type TEXT NOT NULL,
              bytes INTEGER NOT NULL,
              sha256 TEXT NOT NULL,
              width INTEGER NOT NULL,
              height INTEGER NOT NULL,
              error TEXT,
              review_status TEXT NOT NULL DEFAULT 'pending_review',
              review_note TEXT,
              reviewed_at TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              UNIQUE(job_id, variant_id)
            )
            """
        )
        conn.execute(
            """
            INSERT INTO job_outputs (
              id, job_id, variant_id, status, storage_path, content_type,
              bytes, sha256, width, height, created_at, updated_at
            )
            VALUES (
              'output-1', 'job-1', 'catalog_square', 'completed',
              'outputs/catalog_square.jpg', 'image/jpeg', 10, ?, 2000, 2000,
              '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z'
            )
            """,
            ("a" * 64,),
        )

    JobStore(str(db_path)).initialize()

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(job_outputs)")}
        legacy = conn.execute("SELECT * FROM job_outputs").fetchone()

    assert {"cost_usd", "cost_ticks", "model", "latency_ms"} <= columns
    # The pre-existing row backfills to a recorded zero cost, never NULL.
    assert legacy["cost_usd"] == 0
    assert legacy["model"] is None


def test_fail_claimed_job_persists_supplied_failure_category(tmp_path: Path) -> None:
    store = JobStore(str(tmp_path / "aphrodite.db"))
    store.initialize()
    store.create_job(job_request())
    claim = store.claim_next_job(worker_id="renderer")
    assert claim is not None

    failed = store.fail_claimed_job(
        job_id=claim.job.id,
        claim_token=claim.claim_token,
        error="xAI image request timed out",
        failure_category=JobFailureCategory.TIMEOUT,
    )

    assert failed is not None
    assert failed.status == JobStatus.FAILED
    assert failed.failure_category == JobFailureCategory.TIMEOUT
