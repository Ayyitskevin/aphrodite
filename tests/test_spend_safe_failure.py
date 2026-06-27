"""Spend-safe failure: a render error never charges Mise.

A failed render produces no completed output, so it contributes nothing to the
renders projection that Mise sums against its cap. Complements the renderer-level
ledger tests (no charge / released reservation on error) in test_xai_renderer.py
and the worker-level no-complete-on-error test in test_worker.py.
"""

from pathlib import Path

from aphrodite.domain import (
    JobCreate,
    JobOutputCreate,
    JobStatus,
    ProductInput,
    build_render_results,
)
from aphrodite.store import JobStore
from aphrodite.worker import process_next_job


def _job_request(*, quantity: int = 1) -> JobCreate:
    return JobCreate(
        product=ProductInput(name="Mug", source_image_uri="file:///mug.jpg"),
        marketplace_targets=["catalog_square"],
        quantity_per_target=quantity,
    )


def test_failed_job_contributes_no_cost_to_renders_projection(tmp_path: Path) -> None:
    store = JobStore(str(tmp_path / "db.sqlite"))
    store.initialize()
    created = store.create_job(_job_request())
    claim = store.claim_next_job(worker_id="renderer")
    assert claim is not None

    failed = store.fail_claimed_job(
        job_id=created.id,
        claim_token=claim.claim_token,
        error="xAI image request failed",
    )
    assert failed is not None
    assert failed.status == JobStatus.FAILED

    job = store.get_job(created.id)
    assert job is not None
    envelope = build_render_results(job)
    # No completed output -> the contract carries no render and no cost.
    assert envelope.renders == []
    assert sum(render.cost_usd for render in envelope.renders) == 0.0


def test_only_completed_renders_carry_cost_after_partial_failure(tmp_path: Path) -> None:
    store = JobStore(str(tmp_path / "db.sqlite"))
    store.initialize()
    created = store.create_job(_job_request(quantity=2))
    claim = store.claim_next_job(worker_id="renderer")
    assert claim is not None

    done = claim.job.output_plan[0]
    store.complete_job_output(
        job_id=created.id,
        output=JobOutputCreate(
            claim_token=claim.claim_token,
            variant_id=done.id,
            storage_path=f"outputs/{done.id}.jpg",
            content_type="image/jpeg",
            bytes=10,
            sha256="a" * 64,
            width=2000,
            height=2000,
            cost_usd=0.05,
        ),
    )
    # The second variant never renders; the job fails.
    store.fail_claimed_job(
        job_id=created.id, claim_token=claim.claim_token, error="provider error"
    )

    job = store.get_job(created.id)
    assert job is not None
    envelope = build_render_results(job)
    # Only the completed render is charged; the failed variant adds nothing.
    assert len(envelope.renders) == 1
    assert envelope.renders[0].cost_usd == 0.05
    assert sum(render.cost_usd for render in envelope.renders) == 0.05


class _FailingBackend:
    name = "failing"

    def render(self, *, job, variant):
        raise RuntimeError("xAI image request failed")


class _StoreWorkerApi:
    """Adapts the worker's WorkerApi contract onto a real JobStore."""

    def __init__(self, store: JobStore) -> None:
        self.store = store

    def claim_next_job(self, *, worker_id: str, claim_ttl_seconds: int):
        return self.store.claim_next_job(
            worker_id=worker_id, claim_ttl_seconds=claim_ttl_seconds
        )

    def heartbeat(self, *, job_id: str, claim_token: str, claim_ttl_seconds: int):
        return self.store.refresh_claim(
            job_id=job_id, claim_token=claim_token, claim_ttl_seconds=claim_ttl_seconds
        )

    def complete_output(self, *, job_id: str, output_payload: dict):
        record = self.store.complete_job_output(
            job_id=job_id, output=JobOutputCreate.model_validate(output_payload)
        )
        return record.model_dump() if record is not None else {}

    def fail_job(
        self, *, job_id: str, claim_token: str, error_message: str, failure_category=None
    ):
        return self.store.fail_claimed_job(
            job_id=job_id,
            claim_token=claim_token,
            error=error_message,
            failure_category=failure_category,
        )


def test_worker_render_failure_leaves_job_failed_with_no_cost(tmp_path: Path) -> None:
    store = JobStore(str(tmp_path / "db.sqlite"))
    store.initialize()
    created = store.create_job(_job_request())

    processed = process_next_job(
        client=_StoreWorkerApi(store),
        backend=_FailingBackend(),
        worker_id="renderer",
        claim_ttl_seconds=300,
    )
    assert processed is True

    job = store.get_job(created.id)
    assert job is not None
    # End to end: the render raised, the worker failed the job, nothing was
    # completed, and the contract charges Mise nothing.
    assert job.status == JobStatus.FAILED
    assert job.outputs == []
    assert build_render_results(job).renders == []
