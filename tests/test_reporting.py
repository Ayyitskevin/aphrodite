import json
from pathlib import Path

from aphrodite.domain import (
    JobOutputRecord,
    JobRecord,
    JobStatus,
    OutputReviewStatus,
    OutputStatus,
    OutputVariant,
    ProductInput,
    ProjectJobBatchRecord,
)
from aphrodite.reporting import (
    XAISpendEntry,
    XAISpendSummary,
    project_job_batch_report,
    project_job_batch_report_csv,
    read_xai_spend_summary,
)


def test_project_job_batch_report_summarizes_jobs_outputs_and_spend() -> None:
    batch = ProjectJobBatchRecord(
        id="batch-1",
        project_id="project-1",
        source="admin_csv",
        created=2,
        jobs=[
            _job(
                "job-complete",
                status=JobStatus.COMPLETED,
                updated_at="2026-06-24T02:04:00Z",
                outputs=[
                    JobOutputRecord(
                        id="out-1",
                        job_id="job-complete",
                        variant_id="catalog_square",
                        status=OutputStatus.COMPLETED,
                        storage_path="outputs/job-complete/catalog_square.png",
                        content_type="image/png",
                        bytes=67,
                        sha256="a" * 64,
                        width=1,
                        height=1,
                        review_status=OutputReviewStatus.APPROVED,
                        reviewed_at="2026-06-24T02:03:00Z",
                        created_at="2026-06-24T02:01:00Z",
                        updated_at="2026-06-24T02:03:30Z",
                    )
                ],
            ),
            _job(
                "job-failed",
                status=JobStatus.FAILED,
                sku="SKU-2",
                updated_at="2026-06-24T02:05:00Z",
                error="renderer crashed",
            ),
        ],
        created_at="2026-06-24T02:00:00Z",
    )
    spend = XAISpendSummary(
        ledger_path="/tmp/xai-costs.jsonl",
        today_cost_usd=0.06,
        total_cost_usd=0.06,
        today_cost_in_usd_ticks=600_000_000,
        total_cost_in_usd_ticks=600_000_000,
        entries=[
            XAISpendEntry(
                date="2026-06-24",
                recorded_at="2026-06-24T02:02:00Z",
                job_id="job-complete",
                variant_id="catalog_square",
                model="grok-2-image",
                cost_in_usd_ticks=600_000_000,
                cost_usd=0.06,
            ),
            XAISpendEntry(
                date="2026-06-24",
                recorded_at="2026-06-24T02:02:00Z",
                job_id="unrelated",
                variant_id="catalog_square",
                model="grok-2-image",
                cost_in_usd_ticks=900_000_000,
                cost_usd=0.09,
            ),
        ],
    )

    report = project_job_batch_report(batch=batch, spend=spend)
    csv_report = project_job_batch_report_csv(batch=batch, spend=spend)

    assert report.job_count == 2
    assert report.planned_output_count == 2
    assert report.output_count == 1
    assert report.approved_output_count == 1
    assert report.approval_rate == 1.0
    assert report.first_render_at == "2026-06-24T02:01:00Z"
    assert report.last_updated_at == "2026-06-24T02:05:00Z"
    assert report.completed_at == "2026-06-24T02:05:00Z"
    assert report.xai_cost_usd == 0.06
    assert report.status_counts.completed == 1
    assert report.status_counts.failed == 1
    assert "batch_xai_cost_usd" in csv_report
    assert "job-complete" in csv_report
    assert "SKU-2" in csv_report
    assert "renderer crashed" in csv_report
    assert "0.0600" in csv_report


def test_read_xai_spend_summary_reads_valid_rows_newest_first(tmp_path: Path) -> None:
    ledger = tmp_path / "xai-costs.jsonl"
    ledger.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "date": "2026-06-23",
                        "recorded_at": "2026-06-23T01:00:00Z",
                        "job_id": "old",
                        "variant_id": "catalog_square",
                        "model": "grok-2-image",
                        "cost_in_usd_ticks": 100_000_000,
                    }
                ),
                "{not-json",
                json.dumps(
                    {
                        "date": "2026-06-24",
                        "recorded_at": "2026-06-24T01:00:00Z",
                        "job_id": "new",
                        "variant_id": "catalog_square",
                        "model": "grok-2-image",
                        "cost_in_usd_ticks": 600_000_000,
                        "cost_usd": "0.06",
                    }
                ),
                json.dumps({"job_id": "bad", "cost_in_usd_ticks": "nope"}),
            ]
        ),
        encoding="utf-8",
    )

    summary = read_xai_spend_summary(ledger_path=str(ledger), limit=1)

    assert summary.total_cost_usd == 0.07
    assert summary.total_cost_in_usd_ticks == 700_000_000
    assert [entry.job_id for entry in summary.entries] == ["new"]
    assert summary.entries[0].cost_usd == 0.06


def _job(
    job_id: str,
    *,
    status: JobStatus,
    updated_at: str,
    sku: str = "SKU-1",
    outputs: list[JobOutputRecord] | None = None,
    error: str | None = None,
) -> JobRecord:
    return JobRecord(
        id=job_id,
        status=status,
        product=ProductInput(
            name=f"Product {job_id}",
            source_image_uri=f"file:///media/{job_id}.jpg",
            sku=sku,
        ),
        project_id="project-1",
        batch_id="batch-1",
        marketplace_targets=["catalog_square"],
        output_plan=[
            OutputVariant(
                id="catalog_square",
                target_id="catalog_square",
                label="Catalog square",
                width=2000,
                height=2000,
                aspect_ratio="1:1",
                output_format="jpg",
                background="clean_white",
                safe_margin_percent=8,
            )
        ],
        outputs=outputs or [],
        priority=5,
        error=error,
        created_at="2026-06-24T02:00:00Z",
        updated_at=updated_at,
    )
