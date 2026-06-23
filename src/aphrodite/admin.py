"""Small admin read views for Aphrodite operators."""

from __future__ import annotations

# HTML templates live inline for now to avoid adding a template dependency.
# ruff: noqa: E501
import json
from dataclasses import asdict, dataclass
from datetime import date
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import quote

from aphrodite.domain import JobRecord, OutputReviewStatus
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


def render_admin_jobs_index(*, jobs: list[JobRecord], spend: XAISpendSummary) -> str:
    rows = "\n".join(_job_row(job, spend=spend) for job in jobs)
    if not rows:
        rows = '<tr><td colspan="7" class="muted">No jobs yet.</td></tr>'
    return _page(
        title="Aphrodite Jobs",
        body=f"""
        <header>
          <h1>Aphrodite Jobs</h1>
          <nav><a href="/admin/jobs">Jobs</a><a href="/admin/jobs?review=needs_review">Needs review</a><a href="/admin/spend.json">Spend JSON</a></nav>
        </header>
        <section class="metrics">
          <div><span>Today</span><strong>${spend.today_cost_usd:.4f}</strong></div>
          <div><span>Total</span><strong>${spend.total_cost_usd:.4f}</strong></div>
          <div><span>Recent xAI rows</span><strong>{len(spend.entries)}</strong></div>
        </section>
        <section>
          <h2>Recent Jobs</h2>
          <table>
            <thead>
              <tr>
                <th>Product</th>
                <th>Status</th>
                <th>Outputs</th>
                <th>Review</th>
                <th>Worker</th>
                <th>Updated</th>
                <th>Spend</th>
              </tr>
            </thead>
            <tbody>{rows}</tbody>
          </table>
        </section>
        """,
    )


def render_admin_job_detail(*, job: JobRecord, spend: XAISpendSummary) -> str:
    source = _source_asset_panel(job)
    outputs = _outputs_panel(job)
    plan_rows = "\n".join(
        f"""
        <tr>
          <td>{_h(variant.id)}</td>
          <td>{_h(variant.label)}</td>
          <td>{_h(str(variant.width))}x{_h(str(variant.height))}</td>
          <td>{_h(variant.background)}</td>
        </tr>
        """
        for variant in job.output_plan
    )
    spend_rows = "\n".join(
        _spend_row(entry) for entry in spend.entries if entry.job_id == job.id
    )
    if not spend_rows:
        spend_rows = '<tr><td colspan="5" class="muted">No xAI spend recorded for this job.</td></tr>'
    return _page(
        title=f"Aphrodite Job {job.id}",
        body=f"""
        <header>
          <h1>{_h(job.product.name)}</h1>
          <nav>{_job_detail_nav(job)}</nav>
        </header>
        <section class="summary">
          <dl>
            <div><dt>Status</dt><dd><span class="status status-{_h(job.status.value)}">{_h(job.status.value)}</span></dd></div>
            <div><dt>Job ID</dt><dd>{_h(job.id)}</dd></div>
            <div><dt>SKU</dt><dd>{_h(job.product.sku or "-")}</dd></div>
            <div><dt>Worker</dt><dd>{_h(job.claimed_by or "-")}</dd></div>
            <div><dt>Claim expires</dt><dd>{_h(job.claim_expires_at or "-")}</dd></div>
            <div><dt>Updated</dt><dd>{_h(job.updated_at)}</dd></div>
          </dl>
        </section>
        {source}
        {outputs}
        <section>
          <h2>Output Plan</h2>
          <table>
            <thead><tr><th>Variant</th><th>Label</th><th>Size</th><th>Background</th></tr></thead>
            <tbody>{plan_rows}</tbody>
          </table>
        </section>
        <section>
          <h2>xAI Spend</h2>
          <table>
            <thead><tr><th>Recorded</th><th>Variant</th><th>Model</th><th>Ticks</th><th>USD</th></tr></thead>
            <tbody>{spend_rows}</tbody>
          </table>
        </section>
        """,
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


def _job_row(job: JobRecord, *, spend: XAISpendSummary) -> str:
    spend_usd = sum(entry.cost_usd for entry in spend.entries if entry.job_id == job.id)
    review = _review_summary(job)
    return f"""
    <tr>
      <td><a href="/admin/jobs/{_u(job.id)}">{_h(job.product.name)}</a><small>{_h(job.id)}</small></td>
      <td><span class="status status-{_h(job.status.value)}">{_h(job.status.value)}</span></td>
      <td>{len(job.outputs)} / {len(job.output_plan)}</td>
      <td>{review}</td>
      <td>{_h(job.claimed_by or "-")}</td>
      <td>{_h(job.updated_at)}</td>
      <td>${spend_usd:.4f}</td>
    </tr>
    """


def _review_summary(job: JobRecord) -> str:
    pending = sum(
        1 for output in job.outputs if output.review_status == OutputReviewStatus.PENDING_REVIEW
    )
    approved = sum(1 for output in job.outputs if output.review_status == OutputReviewStatus.APPROVED)
    rejected = sum(1 for output in job.outputs if output.review_status == OutputReviewStatus.REJECTED)
    if not job.outputs:
        return '<span class="muted">No outputs</span>'
    chips = []
    if pending:
        chips.append(f'<span class="review review-pending_review">{pending} pending</span>')
    if approved:
        chips.append(f'<span class="review review-approved">{approved} approved</span>')
    if rejected:
        chips.append(f'<span class="review review-rejected">{rejected} rejected</span>')
    return " ".join(chips)


def _job_detail_nav(job: JobRecord) -> str:
    links = [
        '<a href="/admin/jobs">Jobs</a>',
        f'<a href="/v1/jobs/{_u(job.id)}">Job JSON</a>',
    ]
    if any(output.review_status == OutputReviewStatus.APPROVED for output in job.outputs):
        links.append(f'<a href="/admin/jobs/{_u(job.id)}/exports.zip">Export ZIP</a>')
    return "".join(links)


def _source_asset_panel(job: JobRecord) -> str:
    if job.source_asset is None:
        return """
        <section>
          <h2>Source</h2>
          <p class="muted">No uploaded source asset for this job.</p>
        </section>
        """
    asset = job.source_asset
    image = ""
    if asset.content_type.startswith("image/"):
        image = f'<img src="/admin/assets/{_u(asset.id)}/file" alt="Source asset preview">'
    return f"""
    <section>
      <h2>Source</h2>
      <div class="media-grid">
        <figure>{image}<figcaption>{_h(asset.original_filename)}</figcaption></figure>
        <dl>
          <div><dt>Asset ID</dt><dd>{_h(asset.id)}</dd></div>
          <div><dt>Type</dt><dd>{_h(asset.content_type)}</dd></div>
          <div><dt>Size</dt><dd>{asset.width}x{asset.height}</dd></div>
          <div><dt>Bytes</dt><dd>{asset.bytes}</dd></div>
        </dl>
      </div>
    </section>
    """


def _outputs_panel(job: JobRecord) -> str:
    if not job.outputs:
        return """
        <section>
          <h2>Outputs</h2>
          <p class="muted">No completed outputs yet.</p>
        </section>
        """
    figures = []
    for output in job.outputs:
        src = f"/admin/jobs/{_u(job.id)}/outputs/{_u(output.variant_id)}/file"
        preview = ""
        if output.content_type.startswith("image/"):
            preview = f'<img src="{src}" alt="{_h(output.variant_id)} preview">'
        export_link = ""
        if output.review_status == OutputReviewStatus.APPROVED:
            export_link = f'<a class="button" href="/admin/jobs/{_u(job.id)}/outputs/{_u(output.variant_id)}/export">Export</a>'
        note = ""
        if output.review_note:
            note = f'<p class="review-note">{_h(output.review_note)}</p>'
        figures.append(
            f"""
            <figure>
              <a href="{src}">{preview}</a>
              <figcaption>
                <strong>{_h(output.variant_id)}</strong>
                <span>{_h(output.content_type)} - {output.width}x{output.height} - {output.bytes} bytes</span>
                <span class="review review-{_h(output.review_status.value)}">{_h(output.review_status.value.replace("_", " "))}</span>
                <small>Reviewed: {_h(output.reviewed_at or "-")}</small>
                {note}
                <div class="actions">
                  <form method="post" action="/admin/jobs/{_u(job.id)}/outputs/{_u(output.variant_id)}/approve">
                    <button type="submit">Approve</button>
                  </form>
                  <form method="post" action="/admin/jobs/{_u(job.id)}/outputs/{_u(output.variant_id)}/reject">
                    <textarea name="note" maxlength="2000" placeholder="Reason"></textarea>
                    <button type="submit" class="danger">Reject</button>
                  </form>
                  {export_link}
                </div>
              </figcaption>
            </figure>
            """
        )
    zip_link = ""
    if any(output.review_status == OutputReviewStatus.APPROVED for output in job.outputs):
        zip_link = f'<p><a class="button" href="/admin/jobs/{_u(job.id)}/exports.zip">Export approved ZIP</a></p>'
    return f"""
    <section>
      <h2>Outputs</h2>
      {zip_link}
      <div class="media-grid">{''.join(figures)}</div>
    </section>
    """


def _spend_row(entry: XAISpendEntry) -> str:
    return f"""
    <tr>
      <td>{_h(entry.recorded_at or entry.date)}</td>
      <td>{_h(entry.variant_id)}</td>
      <td>{_h(entry.model)}</td>
      <td>{entry.cost_in_usd_ticks}</td>
      <td>${entry.cost_usd:.4f}</td>
    </tr>
    """


def _page(*, title: str, body: str) -> str:
    return f"""
    <!doctype html>
    <html lang="en">
      <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>{_h(title)}</title>
        <style>
          :root {{
            color-scheme: light;
            --bg: #f7f7f4;
            --panel: #ffffff;
            --ink: #1e2528;
            --muted: #687176;
            --line: #d7ddd9;
            --accent: #0f766e;
            --warn: #a16207;
            --bad: #b42318;
          }}
          * {{ box-sizing: border-box; }}
          body {{
            margin: 0;
            background: var(--bg);
            color: var(--ink);
            font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
            font-size: 15px;
            line-height: 1.45;
          }}
          header, section {{ max-width: 1180px; margin: 0 auto; padding: 20px 24px; }}
          header {{
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 16px;
            border-bottom: 1px solid var(--line);
          }}
          h1 {{ margin: 0; font-size: 24px; font-weight: 750; }}
          h2 {{ margin: 0 0 12px; font-size: 17px; }}
          nav {{ display: flex; gap: 12px; flex-wrap: wrap; }}
          a {{ color: var(--accent); text-decoration: none; }}
          a:hover {{ text-decoration: underline; }}
          table {{ width: 100%; border-collapse: collapse; background: var(--panel); border: 1px solid var(--line); }}
          th, td {{ padding: 10px 12px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }}
          th {{ color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: 0; }}
          small, .muted {{ display: block; color: var(--muted); }}
          .metrics {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; }}
          .metrics div {{ background: var(--panel); border: 1px solid var(--line); padding: 14px; }}
          .metrics span {{ display: block; color: var(--muted); font-size: 12px; }}
          .metrics strong {{ display: block; margin-top: 4px; font-size: 22px; }}
          .summary dl, .media-grid dl {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; margin: 0; }}
          dt {{ color: var(--muted); font-size: 12px; }}
          dd {{ margin: 2px 0 0; overflow-wrap: anywhere; }}
          .media-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 16px; }}
          figure {{ margin: 0; background: var(--panel); border: 1px solid var(--line); padding: 10px; }}
          img {{ display: block; width: 100%; max-height: 360px; object-fit: contain; background: #ecefed; }}
          figcaption {{ margin-top: 8px; color: var(--muted); }}
          figcaption strong, figcaption span {{ display: block; }}
          .status, .review {{ display: inline-block; padding: 3px 8px; border: 1px solid var(--line); background: #eef7f5; color: var(--accent); }}
          .status-failed, .review-rejected {{ background: #fff1f0; color: var(--bad); }}
          .status-rendering, .status-queued, .review-pending_review {{ background: #fff7e6; color: var(--warn); }}
          .review-approved {{ background: #eef7f5; color: var(--accent); }}
          .review-note {{ margin: 8px 0 0; color: var(--ink); overflow-wrap: anywhere; }}
          .actions {{ display: grid; gap: 8px; margin-top: 10px; }}
          form {{ display: grid; gap: 6px; margin: 0; }}
          textarea {{ width: 100%; min-height: 56px; resize: vertical; border: 1px solid var(--line); padding: 8px; font: inherit; }}
          button, .button {{ display: inline-block; width: fit-content; border: 1px solid var(--line); background: #eef7f5; color: var(--accent); padding: 6px 10px; font: inherit; cursor: pointer; text-decoration: none; }}
          button:hover, .button:hover {{ text-decoration: none; filter: brightness(0.97); }}
          .danger {{ background: #fff1f0; color: var(--bad); }}
          @media (max-width: 760px) {{
            header {{ align-items: flex-start; flex-direction: column; }}
            .metrics, .summary dl, .media-grid dl {{ grid-template-columns: 1fr; }}
            table {{ display: block; overflow-x: auto; }}
          }}
        </style>
      </head>
      <body>{body}</body>
    </html>
    """


def _ticks_to_usd(ticks: int) -> float:
    return ticks / 10_000_000_000


def _h(value: object) -> str:
    return escape(str(value), quote=True)


def _u(value: object) -> str:
    return quote(str(value), safe="")
