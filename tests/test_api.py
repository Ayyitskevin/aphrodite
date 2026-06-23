from pathlib import Path

from fastapi.testclient import TestClient

from aphrodite.api import create_app
from aphrodite.config import Settings


def client(tmp_path: Path) -> TestClient:
    app = create_app(settings=Settings(db_path=str(tmp_path / "api.db")))
    return TestClient(app)


def test_health_and_readiness(tmp_path: Path) -> None:
    test_client = client(tmp_path)

    assert test_client.get("/healthz").json()["status"] == "ok"
    assert test_client.get("/readiness").json() == {"status": "ready", "store": "sqlite"}


def test_marketplace_presets_are_listed(tmp_path: Path) -> None:
    response = client(tmp_path).get("/v1/marketplace-presets")

    assert response.status_code == 200
    ids = {preset["id"] for preset in response.json()}
    assert {"catalog_square", "transparent_cutout", "social_square"}.issubset(ids)


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
