import sqlite3
from pathlib import Path

from aphrodite.domain import JobCreate, JobOutputCreate, JobStatus, ProductInput
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
    assert failed.claimed_by is None
