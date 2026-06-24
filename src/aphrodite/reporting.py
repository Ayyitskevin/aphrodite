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
    JobRecord,
    JobStatus,
    OutputReviewStatus,
    ProjectJobBatchRecord,
    ProjectJobBatchReport,
    ProjectJobBatchReviewCounts,
    ProjectJobBatchStatusCounts,
)
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


def _ticks_to_usd(ticks: int) -> float:
    return ticks / 10_000_000_000
