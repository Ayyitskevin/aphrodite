from pathlib import Path

from fastapi.testclient import TestClient
from image_fixtures import PNG_1X1, TRUNCATED_PNG

from aphrodite.api import create_app
from aphrodite.assets import AssetStorageError
from aphrodite.config import Settings


def client(tmp_path: Path, *, max_upload_bytes: int = 15_000_000) -> TestClient:
    app = create_app(
        settings=Settings(
            db_path=str(tmp_path / "api.db"),
            media_root=str(tmp_path / "media"),
            max_upload_bytes=max_upload_bytes,
        )
    )
    return TestClient(app)


def test_health_and_readiness(tmp_path: Path) -> None:
    test_client = client(tmp_path)

    assert test_client.get("/healthz").json()["status"] == "ok"
    assert test_client.get("/readiness").json() == {"status": "ready", "store": "sqlite"}


def test_readiness_creates_missing_media_root(tmp_path: Path) -> None:
    media_root = tmp_path / "missing-media"
    app = create_app(
        settings=Settings(db_path=str(tmp_path / "api.db"), media_root=str(media_root))
    )
    test_client = TestClient(app)

    assert test_client.get("/readiness").status_code == 200
    assert media_root.exists()


def test_marketplace_presets_are_listed(tmp_path: Path) -> None:
    response = client(tmp_path).get("/v1/marketplace-presets")

    assert response.status_code == 200
    ids = {preset["id"] for preset in response.json()}
    assert {"catalog_square", "transparent_cutout", "social_square"}.issubset(ids)


def test_upload_asset_and_fetch_metadata(tmp_path: Path) -> None:
    test_client = client(tmp_path)

    response = test_client.post(
        "/v1/assets",
        files={"file": ("mug.png", PNG_1X1, "image/png")},
    )

    assert response.status_code == 201
    asset = response.json()
    assert asset["original_filename"] == "mug.png"
    assert asset["content_type"] == "image/png"
    assert asset["width"] == 1
    assert asset["height"] == 1
    assert (tmp_path / "media" / asset["storage_path"]).exists()

    get_response = test_client.get(f"/v1/assets/{asset['id']}")
    assert get_response.status_code == 200
    assert get_response.json()["sha256"] == asset["sha256"]


def test_upload_asset_rejects_non_image(tmp_path: Path) -> None:
    response = client(tmp_path).post(
        "/v1/assets",
        files={"file": ("notes.txt", b"not an image", "text/plain")},
    )

    assert response.status_code == 415


def test_upload_asset_rejects_corrupt_image_bytes(tmp_path: Path) -> None:
    response = client(tmp_path).post(
        "/v1/assets",
        files={"file": ("mug.png", TRUNCATED_PNG, "image/png")},
    )

    assert response.status_code == 422


def test_upload_asset_storage_error_does_not_create_asset(tmp_path: Path, monkeypatch) -> None:
    test_client = client(tmp_path)

    def fail_write_asset_file(**_kwargs):
        raise AssetStorageError("media root is unavailable")

    monkeypatch.setattr("aphrodite.api.write_asset_file", fail_write_asset_file)

    response = test_client.post(
        "/v1/assets",
        files={"file": ("mug.png", PNG_1X1, "image/png")},
    )

    assert response.status_code == 500
    assert test_client.get("/v1/assets").json() == []


def test_upload_asset_rejects_oversized_image(tmp_path: Path) -> None:
    response = client(tmp_path, max_upload_bytes=8).post(
        "/v1/assets",
        files={"file": ("mug.png", PNG_1X1, "image/png")},
    )

    assert response.status_code == 413


def test_create_clients_projects_and_filter_jobs(tmp_path: Path) -> None:
    test_client = client(tmp_path)

    create_client_response = test_client.post(
        "/v1/clients",
        json={"name": "Maison API", "external_id": "client-api"},
    )
    assert create_client_response.status_code == 201
    client_payload = create_client_response.json()

    create_project_response = test_client.post(
        "/v1/projects",
        json={
            "client_id": client_payload["id"],
            "name": "Summer catalog",
            "external_id": "project-api",
        },
    )
    assert create_project_response.status_code == 201
    project_payload = create_project_response.json()
    assert project_payload["client"]["name"] == "Maison API"

    create_job_response = test_client.post(
        "/v1/jobs",
        json={
            "project_id": project_payload["id"],
            "product": {
                "name": "Canvas tote",
                "source_image_uri": "file:///media/tote/source.jpg",
            },
            "marketplace_targets": ["catalog_square"],
        },
    )
    assert create_job_response.status_code == 201
    job_payload = create_job_response.json()
    assert job_payload["project_id"] == project_payload["id"]
    assert job_payload["project"]["client"]["name"] == "Maison API"

    clients = test_client.get("/v1/clients")
    projects = test_client.get("/v1/projects", params={"client_id": client_payload["id"]})
    jobs_by_client = test_client.get("/v1/jobs", params={"client_id": client_payload["id"]})
    jobs_by_project = test_client.get("/v1/jobs", params={"project_id": project_payload["id"]})

    assert clients.status_code == 200
    assert [item["id"] for item in clients.json()] == [client_payload["id"]]
    assert projects.status_code == 200
    assert [item["id"] for item in projects.json()] == [project_payload["id"]]
    assert jobs_by_client.status_code == 200
    assert [item["id"] for item in jobs_by_client.json()] == [job_payload["id"]]
    assert jobs_by_project.status_code == 200
    assert [item["id"] for item in jobs_by_project.json()] == [job_payload["id"]]


def test_create_project_rejects_missing_client(tmp_path: Path) -> None:
    response = client(tmp_path).post(
        "/v1/projects",
        json={"client_id": "missing", "name": "Missing client project"},
    )

    assert response.status_code == 422
    assert "client not found" in response.json()["detail"]


def test_create_job_rejects_missing_project(tmp_path: Path) -> None:
    response = client(tmp_path).post(
        "/v1/jobs",
        json={
            "project_id": "missing",
            "product": {
                "name": "Canvas tote",
                "source_image_uri": "file:///media/tote/source.jpg",
            },
            "marketplace_targets": ["catalog_square"],
        },
    )

    assert response.status_code == 422
    assert "project not found" in response.json()["detail"]


def test_create_project_job_batch(tmp_path: Path) -> None:
    test_client = client(tmp_path)
    client_payload = test_client.post("/v1/clients", json={"name": "Batch Client"}).json()
    project_payload = test_client.post(
        "/v1/projects",
        json={"client_id": client_payload["id"], "name": "Batch Catalog"},
    ).json()

    response = test_client.post(
        f"/v1/projects/{project_payload['id']}/jobs/batch",
        json={
            "marketplace_targets": ["catalog_square", "transparent_cutout"],
            "priority": 6,
            "items": [
                {
                    "product": {
                        "name": "Batch tote",
                        "sku": "BATCH-001",
                        "source_image_uri": "file:///media/batch/tote.jpg",
                    }
                },
                {
                    "product": {
                        "name": "Batch mug",
                        "source_image_uri": "file:///media/batch/mug.jpg",
                    },
                    "marketplace_targets": ["social_square"],
                    "priority": 8,
                },
            ],
        },
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["project_id"] == project_payload["id"]
    assert payload["source"] == "api"
    assert payload["id"]
    assert payload["created_at"]
    assert payload["created"] == 2
    assert payload["jobs"][0]["batch_id"] == payload["id"]
    assert [job["product"]["name"] for job in payload["jobs"]] == [
        "Batch tote",
        "Batch mug",
    ]
    assert payload["jobs"][0]["project"]["client"]["name"] == "Batch Client"
    assert [variant["target_id"] for variant in payload["jobs"][0]["output_plan"]] == [
        "catalog_square",
        "transparent_cutout",
    ]
    assert [variant["target_id"] for variant in payload["jobs"][1]["output_plan"]] == [
        "social_square",
    ]
    batches = test_client.get(f"/v1/projects/{project_payload['id']}/jobs/batches")
    batch = test_client.get(
        f"/v1/projects/{project_payload['id']}/jobs/batches/{payload['id']}"
    )
    assert batches.status_code == 200
    assert [item["id"] for item in batches.json()] == [payload["id"]]
    assert batch.status_code == 200
    assert batch.json()["jobs"][1]["product"]["name"] == "Batch mug"


def test_project_job_batch_rejects_missing_asset_without_partial_jobs(tmp_path: Path) -> None:
    test_client = client(tmp_path)
    client_payload = test_client.post("/v1/clients", json={"name": "Batch Client"}).json()
    project_payload = test_client.post(
        "/v1/projects",
        json={"client_id": client_payload["id"], "name": "Batch Catalog"},
    ).json()

    response = test_client.post(
        f"/v1/projects/{project_payload['id']}/jobs/batch",
        json={
            "items": [
                {
                    "product": {
                        "name": "Valid item",
                        "source_image_uri": "file:///media/valid.jpg",
                    }
                },
                {
                    "source_asset_id": "missing",
                    "product": {"name": "Missing asset"},
                },
            ],
        },
    )
    jobs = test_client.get("/v1/jobs", params={"project_id": project_payload["id"]})

    assert response.status_code == 422
    assert "source asset not found" in response.json()["detail"]
    assert jobs.status_code == 200
    assert jobs.json() == []


def test_create_project_job_batch_from_csv(tmp_path: Path) -> None:
    test_client = client(tmp_path)
    client_payload = test_client.post("/v1/clients", json={"name": "CSV Client"}).json()
    project_payload = test_client.post(
        "/v1/projects",
        json={"client_id": client_payload["id"], "name": "CSV Catalog"},
    ).json()
    csv_content = (
        b"name,sku,source_image_uri,marketplace_targets,priority\n"
        b"CSV tote,CSV-001,file:///media/csv/tote.jpg,,6\n"
        b"CSV mug,CSV-002,file:///media/csv/mug.jpg,social_square,8\n"
    )

    response = test_client.post(
        f"/v1/projects/{project_payload['id']}/jobs/batch/csv",
        files={"file": ("catalog.csv", csv_content, "text/csv")},
        data={"marketplace_targets": "catalog_square,transparent_cutout", "priority": "5"},
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["created"] == 2
    assert payload["source"] == "api_csv"
    assert [job["product"]["name"] for job in payload["jobs"]] == ["CSV tote", "CSV mug"]
    assert [variant["target_id"] for variant in payload["jobs"][0]["output_plan"]] == [
        "catalog_square",
        "transparent_cutout",
    ]
    assert [variant["target_id"] for variant in payload["jobs"][1]["output_plan"]] == [
        "social_square",
    ]
    assert payload["jobs"][1]["priority"] == 8


def test_project_job_batch_csv_rejects_missing_asset_without_partial_jobs(tmp_path: Path) -> None:
    test_client = client(tmp_path)
    client_payload = test_client.post("/v1/clients", json={"name": "CSV Client"}).json()
    project_payload = test_client.post(
        "/v1/projects",
        json={"client_id": client_payload["id"], "name": "CSV Catalog"},
    ).json()
    csv_content = (
        b"name,source_image_uri,source_asset_id\n"
        b"Valid CSV item,file:///media/valid.jpg,\n"
        b"Missing CSV asset,,missing\n"
    )

    response = test_client.post(
        f"/v1/projects/{project_payload['id']}/jobs/batch/csv",
        files={"file": ("catalog.csv", csv_content, "text/csv")},
    )
    jobs = test_client.get("/v1/jobs", params={"project_id": project_payload["id"]})

    assert response.status_code == 422
    assert "source asset not found" in response.json()["detail"]
    assert jobs.status_code == 200
    assert jobs.json() == []


def test_project_job_batch_csv_rejects_invalid_rows(tmp_path: Path) -> None:
    test_client = client(tmp_path)
    client_payload = test_client.post("/v1/clients", json={"name": "CSV Client"}).json()
    project_payload = test_client.post(
        "/v1/projects",
        json={"client_id": client_payload["id"], "name": "CSV Catalog"},
    ).json()

    response = test_client.post(
        f"/v1/projects/{project_payload['id']}/jobs/batch/csv",
        files={
            "file": (
                "catalog.csv",
                b"name,source_image_uri\n,file:///media/missing-name.jpg\n",
                "text/csv",
            )
        },
    )

    assert response.status_code == 422
    assert "row 2: name is required" in response.json()["detail"]


def test_catalog_import_template_download(tmp_path: Path) -> None:
    response = client(tmp_path).get("/v1/catalog-import/template.csv")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert response.headers["content-disposition"].startswith("attachment;")
    assert response.text.startswith("name,sku,category,instructions,source_image_uri")


def test_project_job_batch_rejects_missing_project(tmp_path: Path) -> None:
    response = client(tmp_path).post(
        "/v1/projects/missing/jobs/batch",
        json={
            "items": [
                {
                    "product": {
                        "name": "Batch item",
                        "source_image_uri": "file:///media/batch/item.jpg",
                    }
                }
            ]
        },
    )

    assert response.status_code == 404


def test_create_get_list_and_update_job(tmp_path: Path) -> None:
    test_client = client(tmp_path)

    payload = {
        "product": {
            "name": "Canvas tote",
            "sku": "TOTE-001",
            "source_image_uri": "file:///media/tote/source.jpg",
        },
        "marketplace_targets": ["catalog_square", "transparent_cutout"],
        "background": {
            "style": "studio_shadow",
            "prompt": "premium studio lighting",
        },
    }
    create_response = test_client.post("/v1/jobs", json=payload)

    assert create_response.status_code == 201
    created = create_response.json()
    assert created["status"] == "queued"
    assert created["source_asset_id"] is None
    assert len(created["output_plan"]) == 2

    get_response = test_client.get(f"/v1/jobs/{created['id']}")
    assert get_response.status_code == 200
    assert get_response.json()["product"]["name"] == "Canvas tote"

    list_response = test_client.get("/v1/jobs", params={"status": "queued"})
    assert list_response.status_code == 200
    assert [job["id"] for job in list_response.json()] == [created["id"]]

    patch_response = test_client.patch(
        f"/v1/jobs/{created['id']}/status",
        json={"status": "rendering"},
    )
    assert patch_response.status_code == 200
    assert patch_response.json()["status"] == "rendering"


def test_create_job_from_uploaded_asset(tmp_path: Path) -> None:
    test_client = client(tmp_path)
    upload = test_client.post(
        "/v1/assets",
        files={"file": ("wallet.png", PNG_1X1, "image/png")},
    ).json()

    response = test_client.post(
        "/v1/jobs",
        json={
            "source_asset_id": upload["id"],
            "product": {
                "name": "Leather wallet",
                "sku": "WALLET-001",
            },
            "marketplace_targets": ["catalog_square"],
        },
    )

    assert response.status_code == 201
    job = response.json()
    assert job["source_asset_id"] == upload["id"]
    assert job["source_asset"]["sha256"] == upload["sha256"]
    assert job["product"]["source_image_uri"] is None


def test_create_job_rejects_missing_asset(tmp_path: Path) -> None:
    response = client(tmp_path).post(
        "/v1/jobs",
        json={
            "source_asset_id": "missing",
            "product": {"name": "Leather wallet"},
            "marketplace_targets": ["catalog_square"],
        },
    )

    assert response.status_code == 422


def test_invalid_job_payload_returns_422(tmp_path: Path) -> None:
    response = client(tmp_path).post(
        "/v1/jobs",
        json={
            "product": {
                "name": "Canvas tote",
                "source_image_uri": "file:///media/tote/source.jpg",
            },
            "marketplace_targets": ["missing"],
        },
    )

    assert response.status_code == 422
