from pathlib import Path

from fastapi.testclient import TestClient

from aphrodite.api import create_app
from aphrodite.config import Settings

API_HEADERS = {"Authorization": "Bearer api-secret"}
WORKER_HEADERS = {"Authorization": "Bearer worker-secret"}


def client(
    tmp_path: Path,
    *,
    api_token: str | None = "api-secret",
    worker_token: str | None = "worker-secret",
) -> TestClient:
    app = create_app(
        settings=Settings(
            db_path=str(tmp_path / "auth.db"),
            media_root=str(tmp_path / "media"),
            api_token=api_token,
            worker_token=worker_token,
        )
    )
    return TestClient(app)


def job_payload() -> dict:
    return {
        "product": {
            "name": "Secure mug",
            "source_image_uri": "file:///media/mug/source.jpg",
        },
        "marketplace_targets": ["catalog_square"],
    }


def test_tokens_unset_leave_local_mutations_open(tmp_path: Path) -> None:
    test_client = client(tmp_path, api_token=None, worker_token=None)

    create_response = test_client.post("/v1/jobs", json=job_payload())
    claim_response = test_client.post(
        "/v1/worker/jobs/claim",
        json={"worker_id": "renderer-a"},
    )

    assert create_response.status_code == 201
    assert claim_response.status_code == 200


def test_api_routes_require_configured_api_token(tmp_path: Path) -> None:
    test_client = client(tmp_path)

    missing = test_client.post("/v1/jobs", json=job_payload())
    wrong = test_client.post(
        "/v1/jobs",
        headers={"Authorization": "Bearer wrong"},
        json=job_payload(),
    )
    worker_token = test_client.post("/v1/jobs", headers=WORKER_HEADERS, json=job_payload())
    authorized = test_client.post("/v1/jobs", headers=API_HEADERS, json=job_payload())

    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert worker_token.status_code == 401
    assert authorized.status_code == 201


def test_worker_routes_require_configured_worker_token(tmp_path: Path) -> None:
    test_client = client(tmp_path)
    test_client.post("/v1/jobs", headers=API_HEADERS, json=job_payload())

    missing = test_client.post(
        "/v1/worker/jobs/claim",
        json={"worker_id": "renderer-a"},
    )
    wrong = test_client.post(
        "/v1/worker/jobs/claim",
        headers={"Authorization": "Bearer wrong"},
        json={"worker_id": "renderer-a"},
    )
    api_token = test_client.post(
        "/v1/worker/jobs/claim",
        headers=API_HEADERS,
        json={"worker_id": "renderer-a"},
    )
    worker_token = test_client.post(
        "/v1/worker/jobs/claim",
        headers=WORKER_HEADERS,
        json={"worker_id": "renderer-a"},
    )

    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert api_token.status_code == 401
    assert worker_token.status_code == 200


def test_api_token_secures_worker_routes_when_worker_token_unset(tmp_path: Path) -> None:
    test_client = client(tmp_path, worker_token=None)
    test_client.post("/v1/jobs", headers=API_HEADERS, json=job_payload())

    response = test_client.post(
        "/v1/worker/jobs/claim",
        headers=API_HEADERS,
        json={"worker_id": "renderer-a"},
    )

    assert response.status_code == 200
