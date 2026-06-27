"""Spend + rights safety: no double-charge on retry, and no auto-publish path."""

import json
import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from aphrodite.alerts import deliver_batch_alert
from aphrodite.api import create_app
from aphrodite.config import Settings
from aphrodite.domain import (
    JobCreate,
    JobOutputCreate,
    OutputReviewStatus,
    ProductInput,
    ProjectJobBatchAlertRecord,
    ProjectJobBatchRecord,
    ProjectRecord,
    render_request_key,
)
from aphrodite.store import JobStore
from aphrodite.worker import HttpWorkerApiClient, _pending_variants


def _job_request(*, quantity: int = 1, idempotency_key: str | None = None) -> JobCreate:
    return JobCreate(
        product=ProductInput(name="Mug", source_image_uri="file:///mug.jpg"),
        marketplace_targets=["catalog_square"],
        quantity_per_target=quantity,
        idempotency_key=idempotency_key,
    )


def _output(claim_token: str, variant_id: str, request_id: str, *, sha: str) -> JobOutputCreate:
    return JobOutputCreate(
        claim_token=claim_token,
        variant_id=variant_id,
        storage_path=f"outputs/{variant_id}.jpg",
        content_type="image/jpeg",
        bytes=1024,
        sha256=sha,
        width=2000,
        height=2000,
        cost_usd=0.02,
        render_request_id=request_id,
    )


def _expire_claim(db_path: Path, job_id: str) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE jobs SET claim_expires_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00Z", job_id),
        )


# --- Request-level idempotency: a re-submitted request does not duplicate ---


def test_resubmitting_same_idempotency_key_returns_same_job(tmp_path: Path) -> None:
    store = JobStore(str(tmp_path / "db.sqlite"))
    store.initialize()

    first = store.create_job(_job_request(idempotency_key="mise-req-1"))
    second = store.create_job(_job_request(idempotency_key="mise-req-1"))

    assert first.id == second.id
    # Exactly one job exists, so no duplicate renders and no double-charge.
    assert len(store.list_jobs(limit=100)) == 1


def test_api_job_create_is_idempotent(tmp_path: Path) -> None:
    client = TestClient(
        create_app(
            settings=Settings(
                db_path=str(tmp_path / "api.db"),
                media_root=str(tmp_path / "media"),
            )
        )
    )
    payload = {
        "product": {"name": "Mug", "source_image_uri": "file:///mug.jpg"},
        "marketplace_targets": ["catalog_square"],
        "idempotency_key": "mise-req-7",
    }

    first = client.post("/v1/jobs", json=payload)
    second = client.post("/v1/jobs", json=payload)

    assert first.status_code == 201 and second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    assert len(client.get("/v1/jobs").json()) == 1


# --- Render-level idempotency: a stable key, and no re-render on retry ---


def test_render_request_key_is_deterministic_and_dedups(tmp_path: Path) -> None:
    store = JobStore(str(tmp_path / "db.sqlite"))
    store.initialize()
    store.create_job(_job_request(quantity=2))
    claim = store.claim_next_job(worker_id="renderer")
    assert claim is not None
    variant = claim.job.output_plan[0]

    key = render_request_key(job=claim.job, variant=variant)
    assert key == render_request_key(job=claim.job, variant=variant)  # deterministic

    store.complete_job_output(
        job_id=claim.job.id, output=_output(claim.claim_token, variant.id, key, sha="a" * 64)
    )
    store.review_output(
        job_id=claim.job.id, variant_id=variant.id, review_status=OutputReviewStatus.APPROVED
    )
    # A re-delivery of the same render (same deterministic key) is a no-op.
    again = store.complete_job_output(
        job_id=claim.job.id, output=_output(claim.claim_token, variant.id, key, sha="a" * 64)
    )

    assert again is not None
    assert again.review_status == OutputReviewStatus.APPROVED
    assert again.cost_usd == 0.02
    job = store.get_job(claim.job.id)
    assert job is not None
    assert sum(1 for o in job.outputs if o.variant_id == variant.id) == 1


def test_retry_reclaim_does_not_rerender_a_completed_variant(tmp_path: Path) -> None:
    db_path = tmp_path / "db.sqlite"
    store = JobStore(str(db_path))
    store.initialize()
    store.create_job(_job_request(quantity=2))
    claim = store.claim_next_job(worker_id="renderer")
    assert claim is not None

    done_variant = claim.job.output_plan[0]
    pending_variant = claim.job.output_plan[1]
    store.complete_job_output(
        job_id=claim.job.id,
        output=_output(
            claim.claim_token,
            done_variant.id,
            render_request_key(job=claim.job, variant=done_variant),
            sha="a" * 64,
        ),
    )

    # Simulate a crashed worker: expire the claim and let another worker recover.
    _expire_claim(db_path, claim.job.id)
    recovered = store.claim_next_job(worker_id="renderer-2")
    assert recovered is not None

    # Only the still-unrendered variant is pending, so the completed one is never
    # re-rendered (and never re-charged) on retry.
    assert [v.id for v in _pending_variants(recovered.job)] == [pending_variant.id]


# --- No auto-publish: nothing leaves the worker for a client automatically ---


def test_worker_client_exposes_no_publish_path() -> None:
    methods = {name for name in dir(HttpWorkerApiClient) if not name.startswith("_")}
    # The worker only claims, heartbeats, completes outputs, and fails jobs.
    assert {"claim_next_job", "heartbeat", "complete_output", "fail_job"} <= methods
    # No outward publish/deliver-to-client surface exists.
    assert not (methods & {"publish", "deliver", "send_to_client", "export", "client_callback"})


def test_completed_render_is_pending_review_not_published(tmp_path: Path) -> None:
    store = JobStore(str(tmp_path / "db.sqlite"))
    store.initialize()
    created = store.create_job(_job_request())
    claim = store.claim_next_job(worker_id="renderer")
    assert claim is not None
    variant = claim.job.output_plan[0]
    store.complete_job_output(
        job_id=created.id,
        output=_output(
            claim.claim_token, variant.id, render_request_key(job=claim.job, variant=variant),
            sha="a" * 64,
        ),
    )

    job = store.get_job(created.id)
    assert job is not None
    # Output is explicit-commit review state, never auto-published.
    assert job.outputs[0].review_status == OutputReviewStatus.PENDING_REVIEW


def test_export_is_blocked_without_approval(tmp_path: Path) -> None:
    settings = Settings(db_path=str(tmp_path / "api.db"), media_root=str(tmp_path / "media"))
    client = TestClient(create_app(settings=settings))
    store = JobStore(settings.db_path)

    created = store.create_job(_job_request())
    claim = store.claim_next_job(worker_id="renderer")
    assert claim is not None
    media_file = Path(settings.media_root) / "outputs" / "catalog_square.jpg"
    media_file.parent.mkdir(parents=True, exist_ok=True)
    media_file.write_bytes(b"fake-jpeg-bytes")
    variant = claim.job.output_plan[0]
    store.complete_job_output(
        job_id=created.id,
        output=_output(
            claim.claim_token, variant.id, render_request_key(job=claim.job, variant=variant),
            sha="a" * 64,
        ),
    )

    # Completed but unapproved: export must be refused, so nothing reaches a client.
    blocked = client.get(f"/admin/jobs/{created.id}/outputs/catalog_square/export")
    assert blocked.status_code == 409


def test_alert_webhook_payload_is_metadata_only(monkeypatch) -> None:
    import aphrodite.alerts as alerts_mod

    captured: dict[str, object] = {}

    def capture(*, settings: Settings, payload: dict) -> None:
        captured["payload"] = payload

    monkeypatch.setattr(alerts_mod, "_post_alert_payload", capture)

    project = ProjectRecord(
        id="p1", client_id="c1", name="Catalog", created_at="t", updated_at="t"
    )
    batch = ProjectJobBatchRecord(
        id="b1", project_id="p1", source="csv_import", created=2, jobs=[], created_at="t"
    )
    alert = ProjectJobBatchAlertRecord(
        id="a1",
        project_id="p1",
        batch_id="b1",
        level="critical",
        code="budget_exceeded_failures",
        message="1 job failed because xAI budget limits were reached.",
        count=1,
        last_seen_at="t",
        created_at="t",
        updated_at="t",
    )

    deliver_batch_alert(
        alert=alert,
        project=project,
        batch=batch,
        settings=Settings(alert_webhook_url="http://example.test/hook"),
    )

    payload = captured["payload"]
    assert set(payload) == {"kind", "service", "environment", "project", "batch", "alert"}
    # No rendered/source media or file references travel in the only outbound feed.
    blob = json.dumps(payload)
    for forbidden in ["storage_path", "sha256", "b64_json", "outputs/", "originals/", "image/"]:
        assert forbidden not in blob
