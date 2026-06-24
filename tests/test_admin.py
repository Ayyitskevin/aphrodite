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


def completed_project_job(
    test_client: TestClient,
    tmp_path: Path,
    *,
    project_id: str,
    name: str,
    sku: str,
) -> dict:
    job = test_client.post(
        "/v1/jobs",
        json={
            "project_id": project_id,
            "product": {
                "name": name,
                "sku": sku,
                "source_image_uri": f"file:///media/{sku.lower()}/source.jpg",
            },
            "marketplace_targets": ["catalog_square"],
        },
    ).json()
    claim = test_client.post(
        "/v1/worker/jobs/claim",
        json={"worker_id": "project-admin-renderer"},
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
    return test_client.get(f"/v1/jobs/{job['id']}").json()


def owned_completed_job(test_client: TestClient, tmp_path: Path) -> tuple[dict, dict, dict]:
    client_payload = test_client.post("/v1/clients", json={"name": "Admin Client"}).json()
    project_payload = test_client.post(
        "/v1/projects",
        json={"client_id": client_payload["id"], "name": "Admin Catalog"},
    ).json()
    upload = test_client.post(
        "/v1/assets",
        files={"file": ("mug.png", PNG_1X1, "image/png")},
    ).json()
    job = test_client.post(
        "/v1/jobs",
        json={
            "source_asset_id": upload["id"],
            "project_id": project_payload["id"],
            "product": {"name": "Owned mug", "sku": "OWNED-001"},
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
    return (
        client_payload,
        project_payload,
        test_client.get(f"/v1/jobs/{job['id']}").json(),
    )


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


def test_admin_project_dashboard_review_and_export_flow(
    tmp_path: Path,
    monkeypatch,
) -> None:
    test_client = client(tmp_path, monkeypatch)
    client_payload = test_client.post("/v1/clients", json={"name": "Project Client"}).json()
    project_payload = test_client.post(
        "/v1/projects",
        json={"client_id": client_payload["id"], "name": "Project Catalog"},
    ).json()
    first_job = completed_project_job(
        test_client,
        tmp_path,
        project_id=project_payload["id"],
        name="Project mug",
        sku="PROJ-MUG",
    )
    second_job = completed_project_job(
        test_client,
        tmp_path,
        project_id=project_payload["id"],
        name="Project tote",
        sku="PROJ-TOTE",
    )

    dashboard = test_client.get(f"/admin/projects/{project_payload['id']}")
    pending = test_client.get(
        f"/admin/projects/{project_payload['id']}",
        params={"review": "pending_review"},
    )
    approve = test_client.post(
        f"/admin/projects/{project_payload['id']}/jobs/{first_job['id']}/outputs/catalog_square/approve",
        params={"review": "pending_review"},
    )
    reject = test_client.post(
        f"/admin/projects/{project_payload['id']}/jobs/{second_job['id']}/outputs/catalog_square/reject",
        data={"note": "Logo clipped"},
    )
    rejected = test_client.get(
        f"/admin/projects/{project_payload['id']}",
        params={"review": "rejected"},
    )
    export = test_client.get(f"/admin/projects/{project_payload['id']}/exports.zip")

    assert dashboard.status_code == 200
    assert "Project Catalog" in dashboard.text
    assert "Review Queue" in dashboard.text
    assert "Project mug" in dashboard.text
    assert "Project tote" in dashboard.text
    assert pending.status_code == 200
    assert "pending review" in pending.text
    assert approve.status_code == 200
    assert reject.status_code == 200
    assert "Logo clipped" in rejected.text
    assert export.status_code == 200
    assert export.headers["content-disposition"].startswith("attachment;")
    with zipfile.ZipFile(io.BytesIO(export.content)) as archive:
        assert len(archive.namelist()) == 1
        assert archive.namelist()[0].endswith("/catalog_square.png")
        assert archive.read(archive.namelist()[0]) == PNG_1X1


def test_admin_project_dashboard_bulk_review_actions(
    tmp_path: Path,
    monkeypatch,
) -> None:
    test_client = client(tmp_path, monkeypatch)
    client_payload = test_client.post("/v1/clients", json={"name": "Bulk Client"}).json()
    project_payload = test_client.post(
        "/v1/projects",
        json={"client_id": client_payload["id"], "name": "Bulk Catalog"},
    ).json()
    approve_first = completed_project_job(
        test_client,
        tmp_path,
        project_id=project_payload["id"],
        name="Bulk mug",
        sku="BULK-MUG",
    )
    approve_second = completed_project_job(
        test_client,
        tmp_path,
        project_id=project_payload["id"],
        name="Bulk tote",
        sku="BULK-TOTE",
    )

    dashboard = test_client.get(f"/admin/projects/{project_payload['id']}")
    approve = test_client.post(
        f"/admin/projects/{project_payload['id']}/outputs/approve-pending"
    )
    approved_jobs = [
        test_client.get(f"/v1/jobs/{approve_first['id']}").json(),
        test_client.get(f"/v1/jobs/{approve_second['id']}").json(),
    ]
    export = test_client.get(f"/admin/projects/{project_payload['id']}/exports.zip")

    assert dashboard.status_code == 200
    assert "2 pending outputs" in dashboard.text
    assert "Approve pending" in dashboard.text
    assert "Reject pending" in dashboard.text
    assert approve.status_code == 200
    assert "Approved 2 pending outputs." in approve.text
    assert "No pending outputs." in approve.text
    assert [
        output["review_status"] for job in approved_jobs for output in job["outputs"]
    ] == ["approved", "approved"]
    assert export.status_code == 200
    with zipfile.ZipFile(io.BytesIO(export.content)) as archive:
        assert len(archive.namelist()) == 2

    reject_first = completed_project_job(
        test_client,
        tmp_path,
        project_id=project_payload["id"],
        name="Bulk plate",
        sku="BULK-PLATE",
    )
    reject_second = completed_project_job(
        test_client,
        tmp_path,
        project_id=project_payload["id"],
        name="Bulk bowl",
        sku="BULK-BOWL",
    )
    reject = test_client.post(
        f"/admin/projects/{project_payload['id']}/outputs/reject-pending",
        data={"note": "Batch rejected"},
    )
    rejected_jobs = [
        test_client.get(f"/v1/jobs/{reject_first['id']}").json(),
        test_client.get(f"/v1/jobs/{reject_second['id']}").json(),
    ]

    assert reject.status_code == 200
    assert "Rejected 2 pending outputs." in reject.text
    assert [
        (output["review_status"], output["review_note"])
        for job in rejected_jobs
        for output in job["outputs"]
    ] == [("rejected", "Batch rejected"), ("rejected", "Batch rejected")]


def test_admin_project_dashboard_blocks_cross_project_review(
    tmp_path: Path,
    monkeypatch,
) -> None:
    test_client = client(tmp_path, monkeypatch)
    client_payload = test_client.post("/v1/clients", json={"name": "Project Client"}).json()
    first_project = test_client.post(
        "/v1/projects",
        json={"client_id": client_payload["id"], "name": "First Catalog"},
    ).json()
    second_project = test_client.post(
        "/v1/projects",
        json={"client_id": client_payload["id"], "name": "Second Catalog"},
    ).json()
    job = completed_project_job(
        test_client,
        tmp_path,
        project_id=first_project["id"],
        name="Cross project mug",
        sku="CROSS-MUG",
    )

    response = test_client.post(
        f"/admin/projects/{second_project['id']}/jobs/{job['id']}/outputs/catalog_square/approve"
    )

    assert response.status_code == 404


def test_admin_import_screen_lists_projects_and_template(tmp_path: Path, monkeypatch) -> None:
    test_client = client(tmp_path, monkeypatch)
    client_payload = test_client.post("/v1/clients", json={"name": "Import Client"}).json()
    project_payload = test_client.post(
        "/v1/projects",
        json={"client_id": client_payload["id"], "name": "Import Catalog"},
    ).json()

    response = test_client.get("/admin/import", params={"project_id": project_payload["id"]})

    assert response.status_code == 200
    assert "Catalog Import" in response.text
    assert "Import Client / Import Catalog" in response.text
    assert f'value="{project_payload["id"]}" selected' in response.text
    assert "/v1/catalog-import/template.csv" in response.text
    assert "Catalog square packshot" in response.text


def test_admin_import_csv_creates_project_batch(tmp_path: Path, monkeypatch) -> None:
    test_client = client(tmp_path, monkeypatch)
    client_payload = test_client.post("/v1/clients", json={"name": "Import Client"}).json()
    project_payload = test_client.post(
        "/v1/projects",
        json={"client_id": client_payload["id"], "name": "Import Catalog"},
    ).json()
    csv_content = (
        b"name,sku,source_image_uri,marketplace_targets,priority\n"
        b"Import tote,IMP-001,file:///media/import/tote.jpg,,6\n"
        b"Import mug,IMP-002,file:///media/import/mug.jpg,social_square,8\n"
    )

    response = test_client.post(
        "/admin/import",
        data={
            "project_id": project_payload["id"],
            "marketplace_targets": ["catalog_square", "transparent_cutout"],
            "background_style": "studio_shadow",
            "quantity_per_target": "1",
            "priority": "5",
        },
        files={"file": ("catalog.csv", csv_content, "text/csv")},
    )
    jobs = test_client.get("/v1/jobs", params={"project_id": project_payload["id"]}).json()

    assert response.status_code == 200
    jobs_by_name = {job["product"]["name"]: job for job in jobs}

    assert "Imported 2 jobs." in response.text
    assert "Open import batch" in response.text
    assert "Import tote" in response.text
    assert "Import mug" in response.text
    assert sorted(jobs_by_name) == ["Import mug", "Import tote"]
    assert [variant["target_id"] for variant in jobs_by_name["Import mug"]["output_plan"]] == [
        "social_square"
    ]
    assert [variant["target_id"] for variant in jobs_by_name["Import tote"]["output_plan"]] == [
        "catalog_square",
        "transparent_cutout",
    ]


def test_admin_project_import_history_and_retry_controls(
    tmp_path: Path,
    monkeypatch,
) -> None:
    test_client = client(tmp_path, monkeypatch)
    client_payload = test_client.post("/v1/clients", json={"name": "History Client"}).json()
    project_payload = test_client.post(
        "/v1/projects",
        json={"client_id": client_payload["id"], "name": "History Catalog"},
    ).json()
    csv_content = (
        b"name,sku,source_image_uri\n"
        b"History tote,HIST-001,file:///media/history/tote.jpg\n"
        b"History mug,HIST-002,file:///media/history/mug.jpg\n"
    )
    import_response = test_client.post(
        "/admin/import",
        data={"project_id": project_payload["id"], "marketplace_targets": ["catalog_square"]},
        files={"file": ("catalog.csv", csv_content, "text/csv")},
    )
    jobs = test_client.get("/v1/jobs", params={"project_id": project_payload["id"]}).json()
    jobs_by_name = {job["product"]["name"]: job for job in jobs}
    batch_id = jobs_by_name["History tote"]["batch_id"]
    failed_job_id = jobs_by_name["History tote"]["id"]
    other_job_id = jobs_by_name["History mug"]["id"]

    fail = test_client.patch(
        f"/v1/jobs/{failed_job_id}/status",
        json={"status": "failed", "error": "renderer crashed"},
    )
    dashboard = test_client.get(f"/admin/projects/{project_payload['id']}")
    detail = test_client.get(f"/admin/projects/{project_payload['id']}/batches/{batch_id}")
    retry_batch = test_client.post(
        f"/admin/projects/{project_payload['id']}/batches/{batch_id}/retry-failed"
    )
    requeued = test_client.get(f"/v1/jobs/{failed_job_id}").json()

    second_fail = test_client.patch(
        f"/v1/jobs/{other_job_id}/status",
        json={"status": "failed", "error": "second crash"},
    )
    retry_project = test_client.post(
        f"/admin/projects/{project_payload['id']}/jobs/retry-failed"
    )
    second_requeued = test_client.get(f"/v1/jobs/{other_job_id}").json()

    assert import_response.status_code == 200
    assert fail.status_code == 200
    assert dashboard.status_code == 200
    assert "Import History" in dashboard.text
    assert "admin csv" in dashboard.text
    assert "Retry failed project jobs" in dashboard.text
    assert "Retry 1 failed job" in dashboard.text
    assert detail.status_code == 200
    assert "Import Batch" in detail.text
    assert "renderer crashed" in detail.text
    assert retry_batch.status_code == 200
    assert "Requeued 1 failed job." in retry_batch.text
    assert requeued["status"] == "queued"
    assert requeued["error"] is None
    assert second_fail.status_code == 200
    assert retry_project.status_code == 200
    assert "Requeued 1 failed job." in retry_project.text
    assert second_requeued["status"] == "queued"


def test_admin_import_csv_requires_marketplace_target(tmp_path: Path, monkeypatch) -> None:
    test_client = client(tmp_path, monkeypatch)
    client_payload = test_client.post("/v1/clients", json={"name": "Import Client"}).json()
    project_payload = test_client.post(
        "/v1/projects",
        json={"client_id": client_payload["id"], "name": "Import Catalog"},
    ).json()

    response = test_client.post(
        "/admin/import",
        data={"project_id": project_payload["id"]},
        files={
            "file": (
                "catalog.csv",
                b"name,source_image_uri\nNo target item,file:///media/no-target.jpg\n",
                "text/csv",
            )
        },
    )
    jobs = test_client.get("/v1/jobs", params={"project_id": project_payload["id"]})

    assert response.status_code == 422
    assert "Select at least one marketplace target." in response.text
    assert 'value="catalog_square" checked' not in response.text
    assert jobs.json() == []


def test_admin_import_csv_renders_errors_without_partial_jobs(
    tmp_path: Path,
    monkeypatch,
) -> None:
    test_client = client(tmp_path, monkeypatch)
    client_payload = test_client.post("/v1/clients", json={"name": "Import Client"}).json()
    project_payload = test_client.post(
        "/v1/projects",
        json={"client_id": client_payload["id"], "name": "Import Catalog"},
    ).json()
    csv_content = (
        b"name,source_image_uri,source_asset_id\n"
        b"Valid import item,file:///media/valid.jpg,\n"
        b"Missing import asset,,missing\n"
    )

    response = test_client.post(
        "/admin/import",
        data={"project_id": project_payload["id"], "marketplace_targets": ["catalog_square"]},
        files={"file": ("catalog.csv", csv_content, "text/csv")},
    )
    jobs = test_client.get("/v1/jobs", params={"project_id": project_payload["id"]})

    assert response.status_code == 422
    assert "source asset not found: missing" in response.text
    assert jobs.status_code == 200
    assert jobs.json() == []


def test_admin_jobs_show_and_filter_ownership(tmp_path: Path, monkeypatch) -> None:
    test_client = client(tmp_path, monkeypatch)
    client_payload, project_payload, job = owned_completed_job(test_client, tmp_path)

    index = test_client.get("/admin/jobs")
    by_client = test_client.get("/admin/jobs", params={"client_id": client_payload["id"]})
    by_project = test_client.get("/admin/jobs", params={"project_id": project_payload["id"]})
    detail = test_client.get(f"/admin/jobs/{job['id']}")
    project_detail = test_client.get(f"/admin/projects/{project_payload['id']}")

    assert index.status_code == 200
    assert "Admin Client" in index.text
    assert "Admin Catalog" in index.text
    assert by_client.status_code == 200
    assert "Owned mug" in by_client.text
    assert by_project.status_code == 200
    assert "Owned mug" in by_project.text
    assert "Open project dashboard" in by_project.text
    assert detail.status_code == 200
    assert "Admin Client" in detail.text
    assert "Admin Catalog" in detail.text
    assert project_detail.status_code == 200
    assert "Owned mug" in project_detail.text


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
    missing_import = test_client.get("/admin/import")
    missing_project = test_client.get("/admin/projects/missing")
    missing_bulk = test_client.post("/admin/projects/missing/outputs/approve-pending")
    authorized = test_client.get("/admin/jobs", headers=API_HEADERS)
    authorized_import = test_client.get("/admin/import", headers=API_HEADERS)
    authorized_project = test_client.get("/admin/projects/missing", headers=API_HEADERS)
    authorized_bulk = test_client.post(
        "/admin/projects/missing/outputs/approve-pending", headers=API_HEADERS
    )

    assert missing.status_code == 401
    assert missing_import.status_code == 401
    assert missing_project.status_code == 401
    assert missing_bulk.status_code == 401
    assert authorized.status_code == 200
    assert authorized_import.status_code == 200
    assert authorized_project.status_code == 404
    assert authorized_bulk.status_code == 404
