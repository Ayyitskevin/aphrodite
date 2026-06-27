from pathlib import Path

from fastapi.testclient import TestClient

from aphrodite.api import create_app
from aphrodite.config import Settings
from aphrodite.domain import JobCreate, JobOutputCreate, OutputReviewStatus, ProductInput
from aphrodite.store import JobStore


def _setup(tmp_path: Path, *, require_rights: bool) -> tuple[TestClient, JobStore, Settings]:
    settings = Settings(
        db_path=str(tmp_path / "api.db"),
        media_root=str(tmp_path / "media"),
        require_rights_confirmation=require_rights,
    )
    app = create_app(settings=settings)
    return TestClient(app), JobStore(settings.db_path), settings


def _seed_approved_output(store: JobStore, media_root: str) -> str:
    job = store.create_job(
        JobCreate(
            product=ProductInput(name="Mug", source_image_uri="file:///mug.jpg"),
            marketplace_targets=["catalog_square"],
        )
    )
    claim = store.claim_next_job(worker_id="renderer")
    assert claim is not None
    media_file = Path(media_root) / "outputs" / "catalog_square.jpg"
    media_file.parent.mkdir(parents=True, exist_ok=True)
    media_file.write_bytes(b"fake-jpeg-bytes")
    store.complete_job_output(
        job_id=job.id,
        output=JobOutputCreate(
            claim_token=claim.claim_token,
            variant_id="catalog_square",
            storage_path="outputs/catalog_square.jpg",
            content_type="image/jpeg",
            bytes=15,
            sha256="a" * 64,
            width=2000,
            height=2000,
            cost_usd=0.02,
        ),
    )
    store.review_output(
        job_id=job.id,
        variant_id="catalog_square",
        review_status=OutputReviewStatus.APPROVED,
    )
    return job.id


def test_export_requires_only_approval_when_policy_off(tmp_path: Path) -> None:
    client, store, settings = _setup(tmp_path, require_rights=False)
    job_id = _seed_approved_output(store, settings.media_root)

    # Default behavior is unchanged: an approved output exports without consent.
    response = client.get(f"/admin/jobs/{job_id}/outputs/catalog_square/export")
    assert response.status_code == 200


def test_export_blocked_until_rights_confirmed_when_policy_on(tmp_path: Path) -> None:
    client, store, settings = _setup(tmp_path, require_rights=True)
    job_id = _seed_approved_output(store, settings.media_root)

    blocked = client.get(f"/admin/jobs/{job_id}/outputs/catalog_square/export")
    assert blocked.status_code == 409
    assert "rights" in blocked.json()["detail"]

    confirm = client.post(
        f"/v1/jobs/{job_id}/outputs/catalog_square/confirm-rights",
        json={"confirmed_by": "studio-owner", "license_note": "model release on file"},
    )
    assert confirm.status_code == 200
    body = confirm.json()
    assert body["rights_confirmed_by"] == "studio-owner"
    assert body["license_note"] == "model release on file"
    assert body["rights_confirmed_at"] is not None

    allowed = client.get(f"/admin/jobs/{job_id}/outputs/catalog_square/export")
    assert allowed.status_code == 200


def test_renders_projection_exposes_rights_state(tmp_path: Path) -> None:
    client, store, settings = _setup(tmp_path, require_rights=True)
    job_id = _seed_approved_output(store, settings.media_root)

    before = client.get(f"/v1/jobs/{job_id}/renders").json()["renders"][0]
    assert before["rights_confirmed"] is False

    client.post(
        f"/v1/jobs/{job_id}/outputs/catalog_square/confirm-rights",
        json={"confirmed_by": "studio-owner"},
    )
    after = client.get(f"/v1/jobs/{job_id}/renders").json()["renders"][0]
    assert after["rights_confirmed"] is True


def test_regenerating_output_clears_rights_confirmation(tmp_path: Path) -> None:
    store = JobStore(str(tmp_path / "db.sqlite"))
    store.initialize()
    store.create_job(
        JobCreate(
            product=ProductInput(name="Mug", source_image_uri="file:///mug.jpg"),
            marketplace_targets=["catalog_square"],
            quantity_per_target=2,
        )
    )
    claim = store.claim_next_job(worker_id="renderer")
    assert claim is not None

    def payload(request_id: str, sha: str) -> JobOutputCreate:
        return JobOutputCreate(
            claim_token=claim.claim_token,
            variant_id="catalog_square-1",
            storage_path="outputs/catalog_square-1.jpg",
            content_type="image/jpeg",
            bytes=10,
            sha256=sha,
            width=2000,
            height=2000,
            render_request_id=request_id,
        )

    store.complete_job_output(job_id=claim.job.id, output=payload("req-1", "a" * 64))
    confirmed = store.confirm_output_rights(
        job_id=claim.job.id,
        variant_id="catalog_square-1",
        confirmed_by="studio-owner",
    )
    assert confirmed is not None and confirmed.rights_confirmed_at is not None

    # A genuinely new render replaces the artifact, so consent must reset: an
    # unconfirmed regenerated image can never inherit the prior confirmation.
    replaced = store.complete_job_output(job_id=claim.job.id, output=payload("req-2", "b" * 64))
    assert replaced is not None
    assert replaced.rights_confirmed_at is None
    assert replaced.rights_confirmed_by is None
