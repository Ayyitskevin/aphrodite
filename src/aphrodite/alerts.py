"""Batch alert persistence and outbound delivery helpers."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from urllib import error, request

from aphrodite.config import Settings
from aphrodite.domain import (
    ProjectJobBatchAlertRecord,
    ProjectJobBatchRecord,
    ProjectRecord,
)
from aphrodite.reporting import (
    project_job_batch_report,
    read_xai_spend_summary,
    xai_cost_ledger_path,
)
from aphrodite.store import JobStore

LOG = logging.getLogger(__name__)


class AlertDeliveryError(Exception):
    """Raised when a configured alert delivery target rejects the notification."""


def process_project_job_batch_alerts(
    *,
    store: JobStore,
    settings: Settings,
    project_id: str,
    batch_id: str,
) -> list[ProjectJobBatchAlertRecord]:
    records = reconcile_project_job_batch_alerts(
        store=store,
        settings=settings,
        project_id=project_id,
        batch_id=batch_id,
    )
    deliver_project_job_batch_alerts(
        store=store,
        settings=settings,
        project_id=project_id,
        batch_id=batch_id,
        records=records,
    )
    return records


def reconcile_project_job_batch_alerts(
    *,
    store: JobStore,
    settings: Settings,
    project_id: str,
    batch_id: str,
) -> list[ProjectJobBatchAlertRecord]:
    batch = store.get_project_job_batch(batch_id)
    if batch is None or batch.project_id != project_id:
        return []
    report = project_job_batch_report(
        batch=batch,
        spend=read_xai_spend_summary(
            ledger_path=xai_cost_ledger_path(media_root=settings.media_root),
            limit=10_000,
        ),
    )
    return store.upsert_project_job_batch_alerts(
        project_id=project_id,
        batch_id=batch_id,
        alerts=report.alerts,
    )


def deliver_project_job_batch_alerts(
    *,
    store: JobStore,
    settings: Settings,
    project_id: str,
    batch_id: str,
    records: list[ProjectJobBatchAlertRecord],
) -> None:
    if not settings.alert_webhook_url:
        return
    project = store.get_project(project_id)
    batch = store.get_project_job_batch(batch_id)
    if project is None or batch is None:
        return

    now = _utc_now()
    for record in records:
        if not _should_deliver(record, now=now):
            continue
        try:
            deliver_batch_alert(
                alert=record,
                project=project,
                batch=batch,
                settings=settings,
            )
        except AlertDeliveryError as exc:
            store.mark_project_job_batch_alert_delivery(
                alert_id=record.id,
                delivered=False,
                error=str(exc),
            )
            LOG.warning(
                "batch alert delivery failed",
                extra={
                    "project_id": project_id,
                    "batch_id": batch_id,
                    "alert_id": record.id,
                    "alert_code": record.code,
                },
            )
            continue
        store.mark_project_job_batch_alert_delivery(
            alert_id=record.id,
            delivered=True,
            error=None,
        )


def deliver_batch_alert(
    *,
    alert: ProjectJobBatchAlertRecord,
    project: ProjectRecord,
    batch: ProjectJobBatchRecord,
    settings: Settings,
) -> None:
    if not settings.alert_webhook_url:
        return
    payload = {
        "service": settings.service_name,
        "environment": settings.env,
        "project": {
            "id": project.id,
            "name": project.name,
            "client_id": project.client_id,
        },
        "batch": {
            "id": batch.id,
            "source": batch.source,
            "created": batch.created,
            "created_at": batch.created_at,
        },
        "alert": alert.model_dump(mode="json"),
    }
    data = json.dumps(payload, sort_keys=True).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if settings.alert_webhook_token:
        headers["Authorization"] = f"Bearer {settings.alert_webhook_token}"
    req = request.Request(
        settings.alert_webhook_url,
        data=data,
        headers=headers,
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=settings.alert_timeout_seconds) as response:
            status_code = getattr(response, "status", 200)
            if status_code >= 400:
                raise AlertDeliveryError(f"webhook returned HTTP {status_code}")
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise AlertDeliveryError(f"webhook returned HTTP {exc.code}: {detail[:240]}") from exc
    except OSError as exc:
        raise AlertDeliveryError(f"webhook request failed: {exc}") from exc


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _should_deliver(alert: ProjectJobBatchAlertRecord, *, now: str) -> bool:
    if alert.level != "critical":
        return False
    if alert.delivery_attempted_at is not None:
        return False
    if alert.acknowledged_at is not None:
        return False
    if alert.muted_until is not None and alert.muted_until > now:
        return False
    if alert.resolved_at is not None:
        return False
    return True
