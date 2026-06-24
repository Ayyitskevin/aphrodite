from pathlib import Path

from fastapi.testclient import TestClient

from aphrodite.api import create_app
from aphrodite.config import Settings


def client(tmp_path: Path) -> TestClient:
    app = create_app(
        settings=Settings(
            db_path=str(tmp_path / "worker-api.db"),
            media_root=str(tmp_path / "media"),
        )
    )
    return TestClient(app)


def create_job(test_client: TestClient) -> str:
    response = test_client.post(
        "/v1/jobs",
        json={
            "product": {
                "name": "Renderer mug",
                "source_image_uri": "file:///media/mug/source.jpg",
            },
            "marketplace_targets": ["catalog_square"],
        },
    )
    assert response.status_code == 201
    return response.json()["id"]


def test_worker_claim_heartbeat_and_complete_flow(tmp_path: Path) -> None:
    test_client = client(tmp_path)
    job_id = create_job(test_client)

    claim_response = test_client.post(
        "/v1/worker/jobs/claim",
        json={"worker_id": "renderer-a", "claim_ttl_seconds": 300},
    )
    assert claim_response.status_code == 200
    claim = claim_response.json()
    assert claim["job"]["id"] == job_id
    assert claim["job"]["status"] == "rendering"
    assert claim["claim_token"]

    heartbeat = test_client.post(
        f"/v1/worker/jobs/{job_id}/heartbeat",
        json={"claim_token": claim["claim_token"], "claim_ttl_seconds": 300},
    )
    assert heartbeat.status_code == 200

    output_response = test_client.post(
        f"/v1/worker/jobs/{job_id}/outputs",
        json={
            "claim_token": claim["claim_token"],
            "variant_id": "catalog_square",
            "storage_path": "outputs/catalog_square.jpg",
            "content_type": "image/jpeg",
            "bytes": 1024,
            "sha256": "b" * 64,
            "width": 2000,
            "height": 2000,
        },
    )
    assert output_response.status_code == 200
    assert output_response.json()["variant_id"] == "catalog_square"

    job_response = test_client.get(f"/v1/jobs/{job_id}")
    assert job_response.status_code == 200
    job = job_response.json()
    assert job["status"] == "completed"
    assert job["outputs"][0]["storage_path"] == "outputs/catalog_square.jpg"


def test_worker_routes_reject_wrong_claim_token(tmp_path: Path) -> None:
    test_client = client(tmp_path)
    job_id = create_job(test_client)
    claim = test_client.post(
        "/v1/worker/jobs/claim",
        json={"worker_id": "renderer-a"},
    ).json()

    heartbeat = test_client.post(
        f"/v1/worker/jobs/{job_id}/heartbeat",
        json={"claim_token": "wrong"},
    )
    output = test_client.post(
        f"/v1/worker/jobs/{job_id}/outputs",
        json={
            "claim_token": "wrong",
            "variant_id": "catalog_square",
            "storage_path": "outputs/catalog_square.jpg",
            "content_type": "image/jpeg",
            "bytes": 1024,
            "sha256": "b" * 64,
            "width": 2000,
            "height": 2000,
        },
    )
    fail = test_client.post(
        f"/v1/worker/jobs/{job_id}/fail",
        json={"claim_token": "wrong", "error": "nope"},
    )

    assert claim["claim_token"] != "wrong"
    assert heartbeat.status_code == 409
    assert output.status_code == 409
    assert fail.status_code == 409



def test_worker_fail_route_accepts_failure_category(tmp_path: Path) -> None:
    test_client = client(tmp_path)
    job_id = create_job(test_client)
    claim = test_client.post(
        "/v1/worker/jobs/claim",
        json={"worker_id": "renderer-a"},
    ).json()

    response = test_client.post(
        f"/v1/worker/jobs/{job_id}/fail",
        json={
            "claim_token": claim["claim_token"],
            "error": "xAI image request timed out",
            "failure_category": "timeout",
        },
    )

    assert response.status_code == 200
    assert response.json()["status"] == "failed"
    assert response.json()["failure_category"] == "timeout"
