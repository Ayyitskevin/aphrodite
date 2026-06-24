"""Batch reporting helpers for Aphrodite operator and API views."""

from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass
from datetime import date
from io import StringIO
from pathlib import Path
from typing import Any

from aphrodite.domain import (
    JobFailureCategory,
    JobRecord,
    JobStatus,
    OutputReviewStatus,
    ProjectJobBatchAlert,
    ProjectJobBatchFailureCounts,
    ProjectJobBatchRecord,
    ProjectJobBatchReport,
    ProjectJobBatchReviewCounts,
    ProjectJobBatchStatusCounts,
)
from aphrodite.failures import classify_failure
from aphrodite.xai import XAIImageConfig


@dataclass(frozen=True, slots=True)
class XAISpendEntry:
    date: str
    recorded_at: str
    job_id: str
    variant_id: str
    model: str
    cost_in_usd_ticks: int
    cost_usd: float

    def as_dict(self) -> dict[str, str | int | float]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class XAISpendSummary:
    ledger_path: str | None
    today_cost_usd: float
    total_cost_usd: float
    today_cost_in_usd_ticks: int
    total_cost_in_usd_ticks: int
    entries: list[XAISpendEntry]

    def as_dict(self) -> dict[str, Any]:
        return {
            "ledger_path": self.ledger_path,
            "today_cost_usd": self.today_cost_usd,
            "total_cost_usd": self.total_cost_usd,
            "today_cost_in_usd_ticks": self.today_cost_in_usd_ticks,
            "total_cost_in_usd_ticks": self.total_cost_in_usd_ticks,
            "entries": [entry.as_dict() for entry in self.entries],
        }


def xai_cost_ledger_path(*, media_root: str) -> str | None:
    return XAIImageConfig.from_env(media_root=media_root).cost_ledger_path


def project_job_batch_report(
    *,
    batch: ProjectJobBatchRecord,
    spend: XAISpendSummary,
) -> ProjectJobBatchReport:
    jobs = batch.jobs
    outputs = [output for job in jobs for output in job.outputs]
    status_counts = _job_status_counts(jobs)
    failure_counts = _job_failure_counts(jobs)
    failure_summary = ProjectJobBatchFailureCounts(
        source_asset_error=failure_counts[JobFailureCategory.SOURCE_ASSET_ERROR],
        provider_error=failure_counts[JobFailureCategory.PROVIDER_ERROR],
        timeout=failure_counts[JobFailureCategory.TIMEOUT],
        budget_exceeded=failure_counts[JobFailureCategory.BUDGET_EXCEEDED],
        renderer_error=failure_counts[JobFailureCategory.RENDERER_ERROR],
        worker_error=failure_counts[JobFailureCategory.WORKER_ERROR],
        unknown=failure_counts[JobFailureCategory.UNKNOWN],
    )
    review_counts = {
        status: sum(1 for output in outputs if output.review_status == status)
        for status in OutputReviewStatus
    }
    job_ids = {job.id for job in jobs}
    spend_entries = [entry for entry in spend.entries if entry.job_id in job_ids]
    spend_ticks = sum(entry.cost_in_usd_ticks for entry in spend_entries)
    output_count = len(outputs)
    approved = review_counts[OutputReviewStatus.APPROVED]
    updated_at_values = [job.updated_at for job in jobs] + [output.updated_at for output in outputs]
    terminal_statuses = {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELED}

    return ProjectJobBatchReport(
        batch_id=batch.id,
        project_id=batch.project_id,
        source=batch.source,
        created_at=batch.created_at,
        first_render_at=min((output.created_at for output in outputs), default=None),
        last_updated_at=max(updated_at_values, default=None),
        completed_at=(
            max((job.updated_at for job in jobs), default=None)
            if jobs and all(job.status in terminal_statuses for job in jobs)
            else None
        ),
        job_count=len(jobs),
        planned_output_count=sum(len(job.output_plan) for job in jobs),
        output_count=output_count,
        pending_review_output_count=review_counts[OutputReviewStatus.PENDING_REVIEW],
        approved_output_count=approved,
        rejected_output_count=review_counts[OutputReviewStatus.REJECTED],
        approval_rate=round(approved / output_count, 4) if output_count else 0.0,
        xai_cost_usd=_ticks_to_usd(spend_ticks),
        xai_cost_in_usd_ticks=spend_ticks,
        status_counts=ProjectJobBatchStatusCounts(
            queued=status_counts[JobStatus.QUEUED],
            planning=status_counts[JobStatus.PLANNING],
            rendering=status_counts[JobStatus.RENDERING],
            completed=status_counts[JobStatus.COMPLETED],
            failed=status_counts[JobStatus.FAILED],
            canceled=status_counts[JobStatus.CANCELED],
        ),
        review_counts=ProjectJobBatchReviewCounts(
            pending_review=review_counts[OutputReviewStatus.PENDING_REVIEW],
            approved=approved,
            rejected=review_counts[OutputReviewStatus.REJECTED],
        ),
        failure_counts=failure_summary,
        alerts=_batch_alerts(
            job_count=len(jobs),
            failed_count=status_counts[JobStatus.FAILED],
            failure_counts=failure_summary,
        ),
    )


def project_job_batch_report_csv(
    *,
    batch: ProjectJobBatchRecord,
    spend: XAISpendSummary,
) -> str:
    report = project_job_batch_report(batch=batch, spend=spend)
    handle = StringIO()
    fieldnames = [
        "batch_id",
        "project_id",
        "source",
        "batch_created_at",
        "batch_completed_at",
        "batch_xai_cost_usd",
        "batch_approval_rate",
        "job_id",
        "product_name",
        "sku",
        "status",
        "failure_category",
        "priority",
        "planned_outputs",
        "outputs",
        "pending_review",
        "approved",
        "rejected",
        "job_xai_cost_usd",
        "updated_at",
        "error",
    ]
    writer = csv.DictWriter(handle, fieldnames=fieldnames)
    writer.writeheader()
    for job in batch.jobs:
        outputs = job.outputs
        pending = sum(
            1 for output in outputs if output.review_status == OutputReviewStatus.PENDING_REVIEW
        )
        approved = sum(
            1 for output in outputs if output.review_status == OutputReviewStatus.APPROVED
        )
        rejected = sum(
            1 for output in outputs if output.review_status == OutputReviewStatus.REJECTED
        )
        job_ticks = sum(
            entry.cost_in_usd_ticks for entry in spend.entries if entry.job_id == job.id
        )
        writer.writerow(
            {
                "batch_id": batch.id,
                "project_id": batch.project_id,
                "source": batch.source,
                "batch_created_at": batch.created_at,
                "batch_completed_at": report.completed_at or "",
                "batch_xai_cost_usd": f"{report.xai_cost_usd:.4f}",
                "batch_approval_rate": f"{report.approval_rate:.4f}",
                "job_id": job.id,
                "product_name": job.product.name,
                "sku": job.product.sku or "",
                "status": job.status.value,
                "failure_category": job.failure_category.value if job.failure_category else "",
                "priority": job.priority,
                "planned_outputs": len(job.output_plan),
                "outputs": len(outputs),
                "pending_review": pending,
                "approved": approved,
                "rejected": rejected,
                "job_xai_cost_usd": f"{_ticks_to_usd(job_ticks):.4f}",
                "updated_at": job.updated_at,
                "error": job.error or "",
            }
        )
    return handle.getvalue()


def read_xai_spend_summary(
    *,
    ledger_path: str | None,
    limit: int = 50,
) -> XAISpendSummary:
    entries = _read_xai_spend_entries(ledger_path)
    today = date.today().isoformat()
    today_entries = [entry for entry in entries if entry.date == today]
    total_ticks = sum(entry.cost_in_usd_ticks for entry in entries)
    today_ticks = sum(entry.cost_in_usd_ticks for entry in today_entries)
    return XAISpendSummary(
        ledger_path=ledger_path,
        today_cost_usd=_ticks_to_usd(today_ticks),
        total_cost_usd=_ticks_to_usd(total_ticks),
        today_cost_in_usd_ticks=today_ticks,
        total_cost_in_usd_ticks=total_ticks,
        entries=entries[: max(0, limit)],
    )


def _read_xai_spend_entries(ledger_path: str | None) -> list[XAISpendEntry]:
    if not ledger_path:
        return []
    path = Path(ledger_path)
    if not path.exists():
        return []

    entries: list[XAISpendEntry] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            entry = _entry_from_payload(payload)
            if entry is not None:
                entries.append(entry)
    return list(reversed(entries))


def _entry_from_payload(payload: Any) -> XAISpendEntry | None:
    if not isinstance(payload, dict):
        return None
    try:
        cost_ticks = int(payload.get("cost_in_usd_ticks") or 0)
    except (TypeError, ValueError):
        return None
    cost_usd = payload.get("cost_usd")
    try:
        normalized_cost_usd = (
            float(cost_usd) if cost_usd is not None else _ticks_to_usd(cost_ticks)
        )
    except (TypeError, ValueError):
        normalized_cost_usd = _ticks_to_usd(cost_ticks)

    return XAISpendEntry(
        date=str(payload.get("date") or ""),
        recorded_at=str(payload.get("recorded_at") or ""),
        job_id=str(payload.get("job_id") or ""),
        variant_id=str(payload.get("variant_id") or ""),
        model=str(payload.get("model") or ""),
        cost_in_usd_ticks=cost_ticks,
        cost_usd=normalized_cost_usd,
    )


def _job_status_counts(jobs: list[JobRecord]) -> dict[JobStatus, int]:
    return {status: sum(1 for job in jobs if job.status == status) for status in JobStatus}


def _job_failure_counts(jobs: list[JobRecord]) -> dict[JobFailureCategory, int]:
    return {
        category: sum(
            1
            for job in jobs
            if job.status == JobStatus.FAILED and _job_failure_category(job) == category
        )
        for category in JobFailureCategory
    }


def _job_failure_category(job: JobRecord) -> JobFailureCategory:
    return job.failure_category or classify_failure(job.error)


def _batch_alerts(
    *,
    job_count: int,
    failed_count: int,
    failure_counts: ProjectJobBatchFailureCounts,
) -> list[ProjectJobBatchAlert]:
    alerts: list[ProjectJobBatchAlert] = []
    if failed_count and failed_count == job_count:
        alerts.append(
            ProjectJobBatchAlert(
                level="critical",
                code="batch_blocked",
                message=f"All {failed_count} jobs in this batch failed.",
                count=failed_count,
            )
        )

    for category, count in _failure_count_items(failure_counts):
        if count == 0:
            continue
        level = (
            "critical"
            if category
            in {JobFailureCategory.BUDGET_EXCEEDED, JobFailureCategory.SOURCE_ASSET_ERROR}
            else "warning"
        )
        alerts.append(
            ProjectJobBatchAlert(
                level=level,
                code=f"{category.value}_failures",
                message=_failure_alert_message(category=category, count=count),
                count=count,
            )
        )
    return alerts


def _failure_count_items(
    failure_counts: ProjectJobBatchFailureCounts,
) -> list[tuple[JobFailureCategory, int]]:
    return [
        (JobFailureCategory.SOURCE_ASSET_ERROR, failure_counts.source_asset_error),
        (JobFailureCategory.PROVIDER_ERROR, failure_counts.provider_error),
        (JobFailureCategory.TIMEOUT, failure_counts.timeout),
        (JobFailureCategory.BUDGET_EXCEEDED, failure_counts.budget_exceeded),
        (JobFailureCategory.RENDERER_ERROR, failure_counts.renderer_error),
        (JobFailureCategory.WORKER_ERROR, failure_counts.worker_error),
        (JobFailureCategory.UNKNOWN, failure_counts.unknown),
    ]


def _failure_alert_message(*, category: JobFailureCategory, count: int) -> str:
    plural = "job" if count == 1 else "jobs"
    labels = {
        JobFailureCategory.SOURCE_ASSET_ERROR: "source asset failures need catalog fixes",
        JobFailureCategory.PROVIDER_ERROR: "provider failures may be transient",
        JobFailureCategory.TIMEOUT: "timeout failures may need retry or longer worker TTL",
        JobFailureCategory.BUDGET_EXCEEDED: "budget failures need xAI budget changes",
        JobFailureCategory.RENDERER_ERROR: "renderer failures need worker/backend review",
        JobFailureCategory.WORKER_ERROR: "worker contract failures need operator review",
        JobFailureCategory.UNKNOWN: "uncategorized failures need triage",
    }
    return f"{count} {plural}: {labels[category]}."


def _ticks_to_usd(ticks: int) -> float:
    return ticks / 10_000_000_000
