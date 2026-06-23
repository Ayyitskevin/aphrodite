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
