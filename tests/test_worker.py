import pytest

from aphrodite.domain import (
    JobRecord,
    JobStatus,
    OutputStatus,
    OutputVariant,
    ProductInput,
    WorkerJobClaim,
)
from aphrodite.renderers import RenderedOutput
from aphrodite.worker import WorkerApiError, WorkerConfig, process_next_job, run_worker


def variant(variant_id: str = "catalog_square") -> OutputVariant:
    return OutputVariant(
        id=variant_id,
        target_id="catalog_square",
        label="Catalog square",
        width=2000,
        height=2000,
        aspect_ratio="1:1",
        output_format="jpg",
        background="clean_white",
        safe_margin_percent=8,
    )


def job(*, outputs: list[dict] | None = None) -> JobRecord:
    payload = {
        "id": "job-123",
        "status": JobStatus.RENDERING,
        "product": ProductInput(name="Matte mug", source_image_uri="file:///mug.jpg"),
        "marketplace_targets": ["catalog_square"],
        "output_plan": [variant("catalog_square"), variant("hero")],
        "priority": 5,
        "claimed_by": "worker-a",
        "claimed_at": "2026-06-23T00:00:00Z",
        "claim_expires_at": "2026-06-23T00:05:00Z",
        "created_at": "2026-06-23T00:00:00Z",
        "updated_at": "2026-06-23T00:00:00Z",
    }
    if outputs is not None:
        payload["outputs"] = outputs
    return JobRecord.model_validate(payload)


def claim(*, claimed_job: JobRecord | None = None, token: str = "claim-token") -> WorkerJobClaim:
    return WorkerJobClaim(
        job=claimed_job or job(),
        claim_token=token,
        claim_expires_at="2026-06-23T00:05:00Z",
    )


class FakeClient:
    def __init__(self, claimed_job: JobRecord | None = None) -> None:
        self.claimed_job = claimed_job
        self.completed: list[dict] = []
        self.failed: list[str] = []
        self.heartbeats = 0

    def claim_next_job(self, *, worker_id: str, claim_ttl_seconds: int) -> WorkerJobClaim | None:
        if self.claimed_job is None:
            return None
        return claim(claimed_job=self.claimed_job)

    def heartbeat(
        self,
        *,
        job_id: str,
        claim_token: str,
        claim_ttl_seconds: int,
    ) -> WorkerJobClaim:
        self.heartbeats += 1
        return claim(claimed_job=self.claimed_job, token=claim_token)

    def complete_output(self, *, job_id: str, output_payload: dict[str, str | int]) -> dict:
        self.completed.append(output_payload)
        return dict(output_payload)

    def fail_job(self, *, job_id: str, claim_token: str, error_message: str) -> JobRecord:
        self.failed.append(error_message)
        return self.claimed_job


class FakeBackend:
    name = "fake"

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.rendered: list[str] = []

    def render(self, *, job: JobRecord, variant: OutputVariant) -> RenderedOutput:
        if self.fail:
            raise RuntimeError("render failed")
        self.rendered.append(variant.id)
        return RenderedOutput(
            variant_id=variant.id,
            storage_path=f"outputs/{job.id}/{variant.id}.jpg",
            content_type="image/jpeg",
            bytes=100,
            sha256="d" * 64,
            width=variant.width,
            height=variant.height,
        )


def completed_output(variant_id: str) -> dict:
    return {
        "id": f"output-{variant_id}",
        "job_id": "job-123",
        "variant_id": variant_id,
        "status": OutputStatus.COMPLETED,
        "storage_path": f"outputs/job-123/{variant_id}.jpg",
        "content_type": "image/jpeg",
        "bytes": 100,
        "sha256": "e" * 64,
        "width": 2000,
        "height": 2000,
        "created_at": "2026-06-23T00:00:00Z",
        "updated_at": "2026-06-23T00:00:00Z",
    }


def test_process_next_job_returns_false_when_no_claim() -> None:
    client = FakeClient(claimed_job=None)
    backend = FakeBackend()

    assert not process_next_job(
        client=client,
        backend=backend,
        worker_id="worker-a",
        claim_ttl_seconds=300,
    )
    assert backend.rendered == []


def test_process_next_job_renders_pending_variants() -> None:
    client = FakeClient(claimed_job=job(outputs=[completed_output("catalog_square")]))
    backend = FakeBackend()

    assert process_next_job(
        client=client,
        backend=backend,
        worker_id="worker-a",
        claim_ttl_seconds=300,
    )

    assert backend.rendered == ["hero"]
    assert client.heartbeats == 1
    assert client.completed[0]["variant_id"] == "hero"
    assert client.failed == []


def test_process_next_job_fails_claim_when_backend_raises() -> None:
    client = FakeClient(claimed_job=job())
    backend = FakeBackend(fail=True)

    assert process_next_job(
        client=client,
        backend=backend,
        worker_id="worker-a",
        claim_ttl_seconds=300,
    )

    assert client.completed == []
    assert "RuntimeError: render failed" in client.failed[0]


def test_run_worker_once_exits_after_one_poll() -> None:
    config = WorkerConfig(once=True, poll_seconds=0)

    assert run_worker(config=config, client=FakeClient(None), backend=FakeBackend()) == 0


def test_run_worker_surfaces_fail_update_errors() -> None:
    class BrokenFailClient(FakeClient):
        def fail_job(self, *, job_id: str, claim_token: str, error_message: str) -> JobRecord:
            raise RuntimeError("cannot fail")

    with pytest.raises(WorkerApiError):
        process_next_job(
            client=BrokenFailClient(claimed_job=job()),
            backend=FakeBackend(fail=True),
            worker_id="worker-a",
            claim_ttl_seconds=300,
        )
