"""Batch alert persistence and outbound delivery helpers."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import UTC, datetime, timedelta
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


def retry_project_job_batch_alert_delivery(
    *,
    store: JobStore,
    settings: Settings,
    project_id: str,
    batch_id: str,
    alert_id: str,
) -> ProjectJobBatchAlertRecord | None:
    record = store.get_project_job_batch_alert(
        project_id=project_id,
        batch_id=batch_id,
        alert_id=alert_id,
    )
    if record is None:
        return None
    attempted = deliver_project_job_batch_alerts(
        store=store,
        settings=settings,
        project_id=project_id,
        batch_id=batch_id,
        records=[record],
        force=True,
    )
    if attempted:
        return attempted[-1]
    return store.get_project_job_batch_alert(
        project_id=project_id,
        batch_id=batch_id,
        alert_id=alert_id,
    )


def deliver_project_job_batch_alerts(
    *,
    store: JobStore,
    settings: Settings,
    project_id: str,
    batch_id: str,
    records: list[ProjectJobBatchAlertRecord],
    force: bool = False,
) -> list[ProjectJobBatchAlertRecord]:
    if not settings.alert_webhook_url:
        return []
    project = store.get_project(project_id)
    batch = store.get_project_job_batch(batch_id)
    if project is None or batch is None:
        return []

    attempted: list[ProjectJobBatchAlertRecord] = []
    now_dt = datetime.now(UTC)
    now = _utc_at(now_dt)
    for record in records:
        if not _should_deliver(record, now=now, force=force):
            continue
        try:
            deliver_batch_alert(
                alert=record,
                project=project,
                batch=batch,
                settings=settings,
            )
        except AlertDeliveryError as exc:
            updated = store.mark_project_job_batch_alert_delivery(
                alert_id=record.id,
                delivered=False,
                error=str(exc),
                next_delivery_attempt_at=_next_retry_at(record, settings=settings, now=now_dt),
            )
            if updated is not None:
                attempted.append(updated)
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
        updated = store.mark_project_job_batch_alert_delivery(
            alert_id=record.id,
            delivered=True,
            error=None,
            next_delivery_attempt_at=None,
        )
        if updated is not None:
            attempted.append(updated)
    return attempted


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
        "kind": "batch_alert",
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
    _post_alert_payload(settings=settings, payload=payload)


def build_alert_digest(
    *,
    store: JobStore,
    settings: Settings,
    limit: int = 100,
) -> dict:
    records = store.list_active_project_job_batch_alerts(limit=limit)
    return {
        "kind": "alert_digest",
        "service": settings.service_name,
        "environment": settings.env,
        "generated_at": _utc_at(datetime.now(UTC)),
        "alert_count": len(records),
        "alerts": [record.model_dump(mode="json") for record in records],
    }


def deliver_alert_digest(
    *,
    store: JobStore,
    settings: Settings,
    limit: int = 100,
) -> dict:
    payload = build_alert_digest(store=store, settings=settings, limit=limit)
    if settings.alert_webhook_url and payload["alert_count"]:
        _post_alert_payload(settings=settings, payload=payload)
    return payload


def _post_alert_payload(*, settings: Settings, payload: dict) -> None:
    if not settings.alert_webhook_url:
        return
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


def _utc_at(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _next_retry_at(
    alert: ProjectJobBatchAlertRecord,
    *,
    settings: Settings,
    now: datetime,
) -> str:
    next_attempt_count = alert.delivery_attempt_count + 1
    delay = settings.alert_retry_base_seconds * (2 ** max(0, next_attempt_count - 1))
    bounded_delay = min(delay, settings.alert_retry_max_seconds)
    return _utc_at(now + timedelta(seconds=bounded_delay))


def _should_deliver(alert: ProjectJobBatchAlertRecord, *, now: str, force: bool) -> bool:
    if alert.level != "critical":
        return False
    if alert.delivered_at is not None:
        return False
    if alert.acknowledged_at is not None:
        return False
    if alert.muted_until is not None and alert.muted_until > now:
        return False
    if alert.resolved_at is not None:
        return False
    if force:
        return True
    if alert.next_delivery_attempt_at is not None and alert.next_delivery_attempt_at > now:
        return False
    return True



def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aphrodite-alerts")
    subparsers = parser.add_subparsers(dest="command", required=True)
    digest = subparsers.add_parser("digest")
    digest.add_argument("--limit", type=int, default=100)
    digest.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    store = JobStore(settings.db_path)
    store.initialize()

    try:
        if args.command == "digest":
            limit = max(1, min(args.limit, 500))
            if args.dry_run:
                payload = build_alert_digest(store=store, settings=settings, limit=limit)
            else:
                payload = deliver_alert_digest(store=store, settings=settings, limit=limit)
            print(json.dumps(payload, sort_keys=True))
            return 0
    except AlertDeliveryError as exc:
        print(f"aphrodite-alerts: {exc}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
