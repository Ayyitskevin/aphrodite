import hashlib
import io
import json
import zipfile
from datetime import date
from pathlib import Path

from fastapi.testclient import TestClient
from image_fixtures import PNG_1X1

from aphrodite.api import create_app
from aphrodite.config import Settings

API_HEADERS = {"Authorization": "Bearer api-secret"}


def client(tmp_path: Path, monkeypatch, *, api_token: str | None = None) -> TestClient:
    monkeypatch.setenv(
        "APHRODITE_XAI_COST_LEDGER_PATH",
        str(tmp_path / "media" / ".xai-costs.jsonl"),
    )
    app = create_app(
        settings=Settings(
            db_path=str(tmp_path / "admin.db"),
            media_root=str(tmp_path / "media"),
            api_token=api_token,
        )
    )
    return TestClient(app)


def completed_job(test_client: TestClient, tmp_path: Path) -> tuple[dict, dict]:
    upload = test_client.post(
        "/v1/assets",
        files={"file": ("mug.png", PNG_1X1, "image/png")},
    ).json()
    job = test_client.post(
        "/v1/jobs",
        json={
            "source_asset_id": upload["id"],
            "product": {"name": "Admin mug", "sku": "ADMIN-001"},
            "marketplace_targets": ["catalog_square"],
        },
    ).json()
    claim = test_client.post(
        "/v1/worker/jobs/claim",
        json={"worker_id": "admin-test-renderer"},
    ).json()

    output_path = f"outputs/{job['id']}/catalog_square.png"
    absolute_output = tmp_path / "media" / output_path
    absolute_output.parent.mkdir(parents=True, exist_ok=True)
    absolute_output.write_bytes(PNG_1X1)

    response = test_client.post(
        f"/v1/worker/jobs/{job['id']}/outputs",
        json={
            "claim_token": claim["claim_token"],
            "variant_id": "catalog_square",
            "storage_path": output_path,
            "content_type": "image/png",
            "bytes": len(PNG_1X1),
            "sha256": hashlib.sha256(PNG_1X1).hexdigest(),
            "width": 1,
            "height": 1,
        },
    )
    assert response.status_code == 200
    return upload, test_client.get(f"/v1/jobs/{job['id']}").json()


def write_spend(tmp_path: Path, *, job_id: str) -> None:
    ledger = tmp_path / "media" / ".xai-costs.jsonl"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(
        "not json\n"
        + json.dumps(
            {
                "date": date.today().isoformat(),
                "recorded_at": "2026-06-23T21:44:35Z",
                "job_id": job_id,
                "variant_id": "catalog_square",
                "model": "grok-imagine-image-quality",
                "cost_in_usd_ticks": 600_000_000,
                "cost_usd": 0.06,
            }
        )
        + "\n",
        encoding="utf-8",
    )


def test_admin_review_and_export_flow(tmp_path: Path, monkeypatch) -> None:
    test_client = client(tmp_path, monkeypatch)
    _asset, job = completed_job(test_client, tmp_path)

    detail = test_client.get(f"/admin/jobs/{job['id']}")
    blocked_export = test_client.get(f"/admin/jobs/{job['id']}/outputs/catalog_square/export")
    needs_review = test_client.get("/admin/jobs?review=needs_review")
    approve = test_client.post(f"/admin/jobs/{job['id']}/outputs/catalog_square/approve")
    approved_job = test_client.get(f"/v1/jobs/{job['id']}").json()
    export = test_client.get(f"/admin/jobs/{job['id']}/outputs/catalog_square/export")
    export_zip = test_client.get(f"/admin/jobs/{job['id']}/exports.zip")
    no_longer_needs_review = test_client.get("/admin/jobs?review=needs_review")

    assert detail.status_code == 200
    assert "pending review" in detail.text
    assert "Approve" in detail.text
    assert "Reject" in detail.text
    assert blocked_export.status_code == 409
    assert needs_review.status_code == 200
    assert "Admin mug" in needs_review.text

    assert approve.status_code == 200
    assert approved_job["outputs"][0]["review_status"] == "approved"
    assert export.status_code == 200
    assert export.headers["content-disposition"].startswith("attachment;")
    assert export.content == PNG_1X1
    assert export_zip.status_code == 200
    assert export_zip.headers["content-type"].startswith("application/zip")
    with zipfile.ZipFile(io.BytesIO(export_zip.content)) as archive:
        assert archive.namelist() == ["catalog_square.png"]
        assert archive.read("catalog_square.png") == PNG_1X1
    assert "Admin mug" not in no_longer_needs_review.text


def test_admin_reject_records_review_note_and_blocks_export(tmp_path: Path, monkeypatch) -> None:
    test_client = client(tmp_path, monkeypatch)
    _asset, job = completed_job(test_client, tmp_path)

    reject = test_client.post(
        f"/admin/jobs/{job['id']}/outputs/catalog_square/reject",
        data={"note": "Logo is clipped"},
    )
    rejected_job = test_client.get(f"/v1/jobs/{job['id']}").json()
    detail = test_client.get(f"/admin/jobs/{job['id']}")
    export = test_client.get(f"/admin/jobs/{job['id']}/outputs/catalog_square/export")

    assert reject.status_code == 200
    assert rejected_job["outputs"][0]["review_status"] == "rejected"
    assert rejected_job["outputs"][0]["review_note"] == "Logo is clipped"
    assert rejected_job["outputs"][0]["reviewed_at"] is not None
    assert detail.status_code == 200
    assert "Logo is clipped" in detail.text
    assert export.status_code == 409


def test_admin_review_missing_output_returns_404(tmp_path: Path, monkeypatch) -> None:
    test_client = client(tmp_path, monkeypatch)
    _asset, job = completed_job(test_client, tmp_path)

    response = test_client.post(f"/admin/jobs/{job['id']}/outputs/missing/approve")

    assert response.status_code == 404


def test_admin_jobs_and_detail_show_completed_job_and_spend(tmp_path: Path, monkeypatch) -> None:
    test_client = client(tmp_path, monkeypatch)
    _asset, job = completed_job(test_client, tmp_path)
    write_spend(tmp_path, job_id=job["id"])

    index = test_client.get("/admin/jobs")
    detail = test_client.get(f"/admin/jobs/{job['id']}")

    assert index.status_code == 200
    assert "Admin mug" in index.text
    assert "completed" in index.text
    assert "$0.0600" in index.text

    assert detail.status_code == 200
    assert "Source" in detail.text
    assert "Outputs" in detail.text
    assert "catalog_square" in detail.text
    assert "grok-imagine-image-quality" in detail.text
    assert f"/admin/assets/{job['source_asset_id']}/file" in detail.text
    assert f"/admin/jobs/{job['id']}/outputs/catalog_square/file" in detail.text


def test_admin_file_preview_routes_serve_verified_media(tmp_path: Path, monkeypatch) -> None:
    test_client = client(tmp_path, monkeypatch)
    asset, job = completed_job(test_client, tmp_path)

    asset_file = test_client.get(f"/admin/assets/{asset['id']}/file")
    output_file = test_client.get(f"/admin/jobs/{job['id']}/outputs/catalog_square/file")

    assert asset_file.status_code == 200
    assert asset_file.headers["content-type"].startswith("image/png")
    assert asset_file.content == PNG_1X1

    assert output_file.status_code == 200
    assert output_file.headers["content-type"].startswith("image/png")
    assert output_file.content == PNG_1X1


def test_admin_output_file_rejects_escaped_storage_path(tmp_path: Path, monkeypatch) -> None:
    test_client = client(tmp_path, monkeypatch)
    job = test_client.post(
        "/v1/jobs",
        json={
            "product": {
                "name": "Unsafe output",
                "source_image_uri": "https://example.test/source.png",
            },
            "marketplace_targets": ["catalog_square"],
        },
    ).json()
    claim = test_client.post(
        "/v1/worker/jobs/claim",
        json={"worker_id": "admin-test-renderer"},
    ).json()
    response = test_client.post(
        f"/v1/worker/jobs/{job['id']}/outputs",
        json={
            "claim_token": claim["claim_token"],
            "variant_id": "catalog_square",
            "storage_path": "../escape.png",
            "content_type": "image/png",
            "bytes": len(PNG_1X1),
            "sha256": hashlib.sha256(PNG_1X1).hexdigest(),
            "width": 1,
            "height": 1,
        },
    )
    assert response.status_code == 200

    preview = test_client.get(f"/admin/jobs/{job['id']}/outputs/catalog_square/file")

    assert preview.status_code == 403


def test_admin_spend_json_ignores_invalid_ledger_rows(tmp_path: Path, monkeypatch) -> None:
    test_client = client(tmp_path, monkeypatch)
    _asset, job = completed_job(test_client, tmp_path)
    write_spend(tmp_path, job_id=job["id"])

    response = test_client.get("/admin/spend.json")

    assert response.status_code == 200
    payload = response.json()
    assert payload["today_cost_usd"] == 0.06
    assert payload["total_cost_in_usd_ticks"] == 600_000_000
    assert len(payload["entries"]) == 1


def test_admin_routes_use_api_token_when_configured(tmp_path: Path, monkeypatch) -> None:
    test_client = client(tmp_path, monkeypatch, api_token="api-secret")

    missing = test_client.get("/admin/jobs")
    authorized = test_client.get("/admin/jobs", headers=API_HEADERS)

    assert missing.status_code == 401
    assert authorized.status_code == 200
