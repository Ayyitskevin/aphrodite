import json
import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from aphrodite.alerts import deliver_alert_digest
from aphrodite.api import create_app
from aphrodite.config import Settings


def client(
    tmp_path: Path,
    *,
    alert_webhook_url: str | None = None,
    alert_webhook_token: str | None = None,
    alert_retry_base_seconds: int = 300,
    alert_retry_max_seconds: int = 3600,
) -> TestClient:
    app = create_app(
        settings=Settings(
            db_path=str(tmp_path / "alerts.db"),
            media_root=str(tmp_path / "media"),
            alert_webhook_url=alert_webhook_url,
            alert_webhook_token=alert_webhook_token,
            alert_timeout_seconds=2,
            alert_retry_base_seconds=alert_retry_base_seconds,
            alert_retry_max_seconds=alert_retry_max_seconds,
        )
    )
    return TestClient(app)


def test_worker_failure_persists_and_delivers_critical_batch_alert_once(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls: list[dict] = []

    class Response:
        status = 204

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    def fake_urlopen(req, timeout: float):
        calls.append(
            {
                "url": req.full_url,
                "authorization": req.get_header("Authorization"),
                "timeout": timeout,
                "payload": json.loads(req.data.decode("utf-8")),
            }
        )
        return Response()

    monkeypatch.setattr("aphrodite.alerts.request.urlopen", fake_urlopen)
    test_client = client(
        tmp_path,
        alert_webhook_url="https://alerts.example.test/aphrodite",
        alert_webhook_token="alert-secret",
    )
    project, batch = _create_batch(test_client, item_count=2)
    failed_job = batch["jobs"][0]
    claim = test_client.post(
        "/v1/worker/jobs/claim",
        json={"worker_id": "alert-renderer"},
    ).json()

    failed = test_client.post(
        f"/v1/worker/jobs/{failed_job['id']}/fail",
        json={
            "claim_token": claim["claim_token"],
            "error": "xAI daily budget would be exceeded by this render",
        },
    )
    repeated = test_client.patch(
        f"/v1/jobs/{failed_job['id']}/status",
        json={
            "status": "failed",
            "error": "xAI daily budget would be exceeded by this render",
        },
    )

    records = test_client.app.state.store.list_project_job_batch_alerts(
        project_id=project["id"],
        batch_id=batch["id"],
    )
    assert failed.status_code == 200
    assert repeated.status_code == 200
    assert len(calls) == 1
    assert calls[0]["url"] == "https://alerts.example.test/aphrodite"
    assert calls[0]["authorization"] == "Bearer alert-secret"
    assert calls[0]["timeout"] == 2
    assert calls[0]["payload"]["alert"]["code"] == "budget_exceeded_failures"
    assert calls[0]["payload"]["project"]["id"] == project["id"]
    assert len(records) == 1
    assert records[0].code == "budget_exceeded_failures"
    assert records[0].delivery_attempted_at is not None
    assert records[0].delivery_attempt_count == 1
    assert records[0].next_delivery_attempt_at is None
    assert records[0].delivered_at is not None


def test_alert_delivery_errors_do_not_fail_worker_response(tmp_path: Path, monkeypatch) -> None:
    def fail_urlopen(_req, timeout: float):
        raise OSError("no route to alerts")

    monkeypatch.setattr("aphrodite.alerts.request.urlopen", fail_urlopen)
    test_client = client(tmp_path, alert_webhook_url="https://alerts.example.test/aphrodite")
    project, batch = _create_batch(test_client, item_count=2)
    failed_job = batch["jobs"][0]
    claim = test_client.post(
        "/v1/worker/jobs/claim",
        json={"worker_id": "alert-renderer"},
    ).json()

    failed = test_client.post(
        f"/v1/worker/jobs/{failed_job['id']}/fail",
        json={
            "claim_token": claim["claim_token"],
            "error": "xAI daily budget would be exceeded by this render",
        },
    )

    records = test_client.app.state.store.list_project_job_batch_alerts(
        project_id=project["id"],
        batch_id=batch["id"],
    )
    assert failed.status_code == 200
    assert records[0].delivery_attempted_at is not None
    assert records[0].delivery_attempt_count == 1
    assert records[0].next_delivery_attempt_at is not None
    assert records[0].delivered_at is None
    assert "no route to alerts" in (records[0].delivery_error or "")


def test_admin_can_acknowledge_and_mute_batch_alerts(tmp_path: Path) -> None:
    test_client = client(tmp_path)
    project, batch = _create_batch(test_client, item_count=2)
    failed_job = batch["jobs"][0]
    claim = test_client.post(
        "/v1/worker/jobs/claim",
        json={"worker_id": "alert-renderer"},
    ).json()
    test_client.post(
        f"/v1/worker/jobs/{failed_job['id']}/fail",
        json={
            "claim_token": claim["claim_token"],
            "error": "xAI daily budget would be exceeded by this render",
        },
    )
    alert = test_client.app.state.store.list_project_job_batch_alerts(
        project_id=project["id"],
        batch_id=batch["id"],
    )[0]

    acknowledged = test_client.post(
        f"/admin/projects/{project['id']}/batches/{batch['id']}/alerts/{alert.id}/acknowledge"
    )
    muted = test_client.post(
        f"/admin/projects/{project['id']}/batches/{batch['id']}/alerts/{alert.id}/mute",
        data={"hours": "12"},
    )
    detail = test_client.get(f"/admin/projects/{project['id']}/batches/{batch['id']}")

    assert acknowledged.status_code == 200
    assert muted.status_code == 200
    assert detail.status_code == 200
    assert "Acknowledged by operator" in detail.text
    assert "Muted until" in detail.text


def test_status_update_reconciles_and_resolves_batch_alerts(tmp_path: Path) -> None:
    test_client = client(tmp_path)
    project, batch = _create_batch(test_client, item_count=2)
    failed_job = batch["jobs"][0]

    failed = test_client.patch(
        f"/v1/jobs/{failed_job['id']}/status",
        json={
            "status": "failed",
            "error": "xAI daily budget would be exceeded by this render",
        },
    )
    active = test_client.app.state.store.list_project_job_batch_alerts(
        project_id=project["id"],
        batch_id=batch["id"],
    )

    recovered = test_client.patch(
        f"/v1/jobs/{failed_job['id']}/status",
        json={"status": "queued"},
    )
    active_after_recovery = test_client.app.state.store.list_project_job_batch_alerts(
        project_id=project["id"],
        batch_id=batch["id"],
    )
    history = test_client.app.state.store.list_project_job_batch_alerts(
        project_id=project["id"],
        batch_id=batch["id"],
        include_resolved=True,
    )

    assert failed.status_code == 200
    assert recovered.status_code == 200
    assert [alert.code for alert in active] == ["budget_exceeded_failures"]
    assert active_after_recovery == []
    assert len(history) == 1
    assert history[0].resolved_at is not None


def test_failed_alert_delivery_waits_for_retry_or_manual_retry(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls: list[str] = []

    class Response:
        status = 204

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    def fake_urlopen(req, timeout: float):
        calls.append(req.full_url)
        if len(calls) == 1:
            raise OSError("alerts unavailable")
        return Response()

    monkeypatch.setattr("aphrodite.alerts.request.urlopen", fake_urlopen)
    test_client = client(
        tmp_path,
        alert_webhook_url="https://alerts.example.test/aphrodite",
        alert_retry_base_seconds=60,
    )
    project, batch = _create_batch(test_client, item_count=2)
    failed_job = batch["jobs"][0]
    claim = test_client.post(
        "/v1/worker/jobs/claim",
        json={"worker_id": "alert-renderer"},
    ).json()

    failed = test_client.post(
        f"/v1/worker/jobs/{failed_job['id']}/fail",
        json={
            "claim_token": claim["claim_token"],
            "error": "xAI daily budget would be exceeded by this render",
        },
    )
    repeated = test_client.patch(
        f"/v1/jobs/{failed_job['id']}/status",
        json={
            "status": "failed",
            "error": "xAI daily budget would be exceeded by this render",
        },
    )
    alert = test_client.app.state.store.list_project_job_batch_alerts(
        project_id=project["id"],
        batch_id=batch["id"],
    )[0]

    retried = test_client.post(
        f"/admin/projects/{project['id']}/batches/{batch['id']}/alerts/{alert.id}/retry-delivery"
    )
    delivered = test_client.app.state.store.list_project_job_batch_alerts(
        project_id=project["id"],
        batch_id=batch["id"],
    )[0]

    assert failed.status_code == 200
    assert repeated.status_code == 200
    assert len(calls) == 2
    assert alert.delivery_attempt_count == 1
    assert alert.next_delivery_attempt_at is not None
    assert retried.status_code == 200
    assert "Retried alert delivery" in retried.text
    assert delivered.delivery_attempt_count == 2
    assert delivered.delivered_at is not None
    assert delivered.delivery_error is None
    assert delivered.next_delivery_attempt_at is None


def test_due_failed_alert_delivery_retries_on_reconciliation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls: list[str] = []

    class Response:
        status = 204

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    def fake_urlopen(req, timeout: float):
        calls.append(req.full_url)
        if len(calls) == 1:
            raise OSError("alerts unavailable")
        return Response()

    monkeypatch.setattr("aphrodite.alerts.request.urlopen", fake_urlopen)
    db_path = tmp_path / "alerts.db"
    test_client = create_app(
        settings=Settings(
            db_path=str(db_path),
            media_root=str(tmp_path / "media"),
            alert_webhook_url="https://alerts.example.test/aphrodite",
            alert_retry_base_seconds=60,
        )
    )
    api = TestClient(test_client)
    project, batch = _create_batch(api, item_count=2)
    failed_job = batch["jobs"][0]

    api.patch(
        f"/v1/jobs/{failed_job['id']}/status",
        json={
            "status": "failed",
            "error": "xAI daily budget would be exceeded by this render",
        },
    )
    api.patch(
        f"/v1/jobs/{failed_job['id']}/status",
        json={
            "status": "failed",
            "error": "xAI daily budget would be exceeded by this render",
        },
    )
    assert len(calls) == 1

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE project_job_batch_alerts SET next_delivery_attempt_at = ?",
            ("2000-01-01T00:00:00Z",),
        )

    retried = api.patch(
        f"/v1/jobs/{failed_job['id']}/status",
        json={
            "status": "failed",
            "error": "xAI daily budget would be exceeded by this render",
        },
    )
    delivered = api.app.state.store.list_project_job_batch_alerts(
        project_id=project["id"],
        batch_id=batch["id"],
    )[0]

    assert retried.status_code == 200
    assert len(calls) == 2
    assert delivered.delivery_attempt_count == 2
    assert delivered.delivered_at is not None


def test_admin_filters_resolved_alerts_and_clears_mute(tmp_path: Path) -> None:
    test_client = client(tmp_path)
    project, batch = _create_batch(test_client, item_count=2)
    failed_job = batch["jobs"][0]

    test_client.patch(
        f"/v1/jobs/{failed_job['id']}/status",
        json={
            "status": "failed",
            "error": "xAI daily budget would be exceeded by this render",
        },
    )
    alert = test_client.app.state.store.list_project_job_batch_alerts(
        project_id=project["id"],
        batch_id=batch["id"],
    )[0]
    active = test_client.get(f"/admin/projects/{project['id']}/batches/{batch['id']}")

    muted = test_client.post(
        f"/admin/projects/{project['id']}/batches/{batch['id']}/alerts/{alert.id}/mute",
        data={"hours": "12"},
    )
    muted_detail = test_client.get(f"/admin/projects/{project['id']}/batches/{batch['id']}")
    cleared = test_client.post(
        f"/admin/projects/{project['id']}/batches/{batch['id']}/alerts/{alert.id}/clear-mute"
    )
    cleared_detail = test_client.get(f"/admin/projects/{project['id']}/batches/{batch['id']}")

    test_client.patch(
        f"/v1/jobs/{failed_job['id']}/status",
        json={"status": "queued"},
    )
    active_after_resolution = test_client.get(
        f"/admin/projects/{project['id']}/batches/{batch['id']}"
    )
    resolved = test_client.get(
        f"/admin/projects/{project['id']}/batches/{batch['id']}?alerts=resolved"
    )

    assert active.status_code == 200
    assert "Retry delivery" in active.text
    assert muted.status_code == 200
    assert "Clear mute" in muted_detail.text
    assert cleared.status_code == 200
    assert "Mute" in cleared_detail.text
    assert "budget exceeded failures" not in active_after_resolution.text
    assert "budget exceeded failures" in resolved.text
    assert "Resolved at" in resolved.text
    assert "Retry delivery" not in resolved.text
    assert "Acknowledge" not in resolved.text


def test_alert_digest_posts_active_alerts(tmp_path: Path, monkeypatch) -> None:
    calls: list[dict] = []

    class Response:
        status = 204

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    def fake_urlopen(req, timeout: float):
        calls.append(
            {
                "url": req.full_url,
                "payload": json.loads(req.data.decode("utf-8")),
                "timeout": timeout,
            }
        )
        return Response()

    monkeypatch.setattr("aphrodite.alerts.request.urlopen", fake_urlopen)
    test_client = client(tmp_path)
    project, batch = _create_batch(test_client, item_count=2)
    failed_job = batch["jobs"][0]
    test_client.patch(
        f"/v1/jobs/{failed_job['id']}/status",
        json={
            "status": "failed",
            "error": "xAI daily budget would be exceeded by this render",
        },
    )

    payload = deliver_alert_digest(
        store=test_client.app.state.store,
        settings=Settings(
            db_path=str(tmp_path / "alerts.db"),
            media_root=str(tmp_path / "media"),
            alert_webhook_url="https://alerts.example.test/digest",
            alert_timeout_seconds=2,
        ),
    )

    assert payload["kind"] == "alert_digest"
    assert payload["alert_count"] == 1
    assert calls[0]["url"] == "https://alerts.example.test/digest"
    assert calls[0]["payload"]["alerts"][0]["batch_id"] == batch["id"]
    assert calls[0]["payload"]["alerts"][0]["project_id"] == project["id"]
    assert calls[0]["timeout"] == 2


def _create_batch(test_client: TestClient, *, item_count: int) -> tuple[dict, dict]:
    owner = test_client.post("/v1/clients", json={"name": "Alert Client"}).json()
    project = test_client.post(
        "/v1/projects",
        json={"client_id": owner["id"], "name": "Alert Catalog"},
    ).json()
    items = [
        {
            "product": {
                "name": f"Alert item {index}",
                "sku": f"ALERT-{index}",
                "source_image_uri": f"file:///media/alert-{index}.jpg",
            }
        }
        for index in range(item_count)
    ]
    batch = test_client.post(
        f"/v1/projects/{project['id']}/jobs/batch",
        json={"items": items},
    ).json()
    return project, batch
