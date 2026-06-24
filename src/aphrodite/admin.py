"""Small admin read views for Aphrodite operators."""

from __future__ import annotations

# HTML templates live inline for now to avoid adding a template dependency.
# ruff: noqa: E501
from html import escape
from pathlib import Path
from urllib.parse import quote

from aphrodite.domain import (
    JobFailureCategory,
    JobOutputRecord,
    JobRecord,
    JobStatus,
    OutputReviewStatus,
    ProjectJobBatchAlertRecord,
    ProjectJobBatchRecord,
    ProjectRecord,
)
from aphrodite.marketplaces import MarketplaceSpec
from aphrodite.reporting import (
    XAISpendEntry,
    XAISpendSummary,
    project_job_batch_report,
)


def render_admin_jobs_index(
    *,
    jobs: list[JobRecord],
    spend: XAISpendSummary,
    active_project: ProjectRecord | None = None,
) -> str:
    rows = "\n".join(_job_row(job, spend=spend) for job in jobs)
    if not rows:
        rows = '<tr><td colspan="8" class="muted">No jobs yet.</td></tr>'
    project_banner = _project_admin_banner(active_project) if active_project is not None else ""
    return _page(
        title="Aphrodite Jobs",
        body=f"""
        <header>
          <h1>Aphrodite Jobs</h1>
          <nav><a href="/admin/jobs">Jobs</a><a href="/admin/import">Import CSV</a><a href="/admin/jobs?review=needs_review">Needs review</a><a href="/admin/spend.json">Spend JSON</a></nav>
        </header>
        <section class="metrics">
          <div><span>Today</span><strong>${spend.today_cost_usd:.4f}</strong></div>
          <div><span>Total</span><strong>${spend.total_cost_usd:.4f}</strong></div>
          <div><span>Recent xAI rows</span><strong>{len(spend.entries)}</strong></div>
        </section>
        {project_banner}
        <section>
          <h2>Recent Jobs</h2>
          <table>
            <thead>
              <tr>
                <th>Product</th>
                <th>Status</th>
                <th>Owner</th>
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


def render_admin_project_detail(
    *,
    project: ProjectRecord,
    jobs: list[JobRecord],
    spend: XAISpendSummary,
    batches: list[ProjectJobBatchRecord] | None = None,
    review_filter: OutputReviewStatus | None = None,
    message: str | None = None,
) -> str:
    output_rows = "\n".join(
        _project_output_row(
            project=project,
            job=job,
            output=output,
            review_filter=review_filter,
        )
        for job, output in _project_review_outputs(jobs=jobs, review_filter=review_filter)
    )
    if not output_rows:
        output_rows = '<tr><td colspan="8" class="muted">No outputs match this view.</td></tr>'

    job_rows = "\n".join(_project_job_row(job) for job in jobs)
    if not job_rows:
        job_rows = '<tr><td colspan="7" class="muted">No jobs for this project yet.</td></tr>'

    message_banner = ""
    if message:
        message_banner = f'<section><div class="alert alert-success">{_h(message)}</div></section>'
    batch_history = _project_batch_history(
        project=project,
        batches=batches or [],
        jobs=jobs,
        spend=spend,
    )

    export_link = ""
    if _project_approved_output_count(jobs):
        export_link = (
            f'<a class="button" href="/admin/projects/{_u(project.id)}/exports.zip">'
            "Export approved ZIP</a>"
        )
    return _page(
        title=f"Aphrodite Project {project.name}",
        body=f"""
        <header>
          <h1>{_h(project.name)}</h1>
          <nav><a href="/admin/jobs">Jobs</a><a href="/admin/import?project_id={_u(project.id)}">Import CSV</a><a href="/admin/jobs?project_id={_u(project.id)}">Job list</a>{export_link}</nav>
        </header>
        {message_banner}
        <section class="summary">
          <dl>
            <div><dt>Client</dt><dd>{_project_client_link(project)}</dd></div>
            <div><dt>Project ID</dt><dd>{_h(project.id)}</dd></div>
            <div><dt>External ID</dt><dd>{_h(project.external_id or "-")}</dd></div>
            <div><dt>Created</dt><dd>{_h(project.created_at)}</dd></div>
            <div><dt>Updated</dt><dd>{_h(project.updated_at)}</dd></div>
            <div><dt>xAI spend</dt><dd>${_project_spend_usd(jobs=jobs, spend=spend):.4f}</dd></div>
          </dl>
        </section>
        {_project_metrics(jobs)}
        <section>
          <h2>Import History</h2>
          {batch_history}
        </section>
        <section>
          <h2>Review Queue</h2>
          {_project_review_nav(project=project, review_filter=review_filter)}
          {_project_bulk_review_controls(project=project, jobs=jobs)}
          <table>
            <thead><tr><th>Product</th><th>Variant</th><th>Review</th><th>Output</th><th>Size</th><th>Updated</th><th>Note</th><th>Actions</th></tr></thead>
            <tbody>{output_rows}</tbody>
          </table>
        </section>
        <section>
          <h2>Project Jobs</h2>
          <table>
            <thead><tr><th>Product</th><th>Status</th><th>Failure</th><th>Outputs</th><th>Review</th><th>Priority</th><th>Updated</th></tr></thead>
            <tbody>{job_rows}</tbody>
          </table>
        </section>
        """,
    )


def render_admin_project_batch_detail(
    *,
    project: ProjectRecord,
    batch: ProjectJobBatchRecord,
    spend: XAISpendSummary,
    alert_records: list[ProjectJobBatchAlertRecord] | None = None,
    message: str | None = None,
) -> str:
    message_banner = ""
    if message:
        message_banner = f'<section><div class="alert alert-success">{_h(message)}</div></section>'
    report = project_job_batch_report(batch=batch, spend=spend)
    job_rows = "\n".join(_batch_job_row(job) for job in batch.jobs)
    if not job_rows:
        job_rows = '<tr><td colspan="8" class="muted">No jobs in this batch.</td></tr>'
    alert_panel = _batch_alert_panel(project=project, batch=batch, report=report, alert_records=alert_records or [])
    retry_action = _batch_retry_action(project=project, batch=batch)
    report_links = _batch_report_links(project=project, batch=batch)
    return _page(
        title=f"Aphrodite Batch {batch.id}",
        body=f"""
        <header>
          <h1>Import Batch</h1>
          <nav><a href="/admin/projects/{_u(project.id)}">Project dashboard</a><a href="/admin/import?project_id={_u(project.id)}">Import CSV</a><a href="/admin/jobs?project_id={_u(project.id)}">Job list</a>{report_links}</nav>
        </header>
        {message_banner}
        <section class="summary">
          <dl>
            <div><dt>Project</dt><dd><a href="/admin/projects/{_u(project.id)}">{_h(project.name)}</a></dd></div>
            <div><dt>Batch ID</dt><dd>{_h(batch.id)}</dd></div>
            <div><dt>Source</dt><dd>{_h(_batch_source_label(batch.source))}</dd></div>
            <div><dt>Jobs</dt><dd>{report.job_count}</dd></div>
            <div><dt>Created</dt><dd>{_h(report.created_at)}</dd></div>
            <div><dt>First render</dt><dd>{_h(report.first_render_at or "-")}</dd></div>
            <div><dt>Last update</dt><dd>{_h(report.last_updated_at or "-")}</dd></div>
            <div><dt>Completed</dt><dd>{_h(report.completed_at or "-")}</dd></div>
            <div><dt>xAI spend</dt><dd>${report.xai_cost_usd:.4f}</dd></div>
          </dl>
        </section>
        <section class="metrics metrics-wide">
          <div><span>Queued</span><strong>{report.status_counts.queued}</strong></div>
          <div><span>Rendering</span><strong>{report.status_counts.rendering}</strong></div>
          <div><span>Completed</span><strong>{report.status_counts.completed}</strong></div>
          <div><span>Failed</span><strong>{report.status_counts.failed}</strong></div>
          <div><span>Outputs</span><strong>{report.output_count} / {report.planned_output_count}</strong></div>
          <div><span>Approved</span><strong>{report.approved_output_count}</strong></div>
          <div><span>Approval</span><strong>{_format_percent(report.approval_rate)}</strong></div>
          <div><span>Spend</span><strong>${report.xai_cost_usd:.4f}</strong></div>
          <div><span>Alerts</span><strong>{len(report.alerts)}</strong></div>
        </section>
        <section>
          <h2>Alerts</h2>
          {alert_panel}
        </section>
        <section>
          <h2>Retry</h2>
          {retry_action}
        </section>
        <section>
          <h2>Batch Jobs</h2>
          <table>
            <thead><tr><th>Product</th><th>Status</th><th>Failure</th><th>Outputs</th><th>Review</th><th>Priority</th><th>Updated</th><th>Error</th></tr></thead>
            <tbody>{job_rows}</tbody>
          </table>
        </section>
        """,
    )


def render_admin_catalog_import(
    *,
    projects: list[ProjectRecord],
    marketplace_specs: list[MarketplaceSpec],
    selected_project_id: str | None = None,
    selected_targets: list[str] | None = None,
    background_style: str = "clean_white",
    background_prompt: str | None = None,
    quantity_per_target: int = 1,
    priority: int = 5,
    result: ProjectJobBatchRecord | None = None,
    error: str | None = None,
) -> str:
    selected = set(["catalog_square"] if selected_targets is None else selected_targets)
    project_options = "\n".join(
        _project_option(project, selected=project.id == selected_project_id)
        for project in projects
    )
    if not project_options:
        project_options = '<option value="">No projects available</option>'

    target_options = "\n".join(
        _marketplace_checkbox(spec, checked=spec.id in selected)
        for spec in marketplace_specs
    )
    background_options = "\n".join(
        _option(value, label, selected=value == background_style)
        for value, label in [
            ("clean_white", "Clean white"),
            ("transparent", "Transparent"),
            ("studio_shadow", "Studio shadow"),
            ("lifestyle", "Lifestyle"),
            ("brand_gradient", "Brand gradient"),
        ]
    )
    banner = ""
    if error:
        banner = f'<section><div class="alert alert-error">{_h(error)}</div></section>'
    elif result is not None:
        plural = "job" if result.created == 1 else "jobs"
        banner = (
            f'<section><div class="alert alert-success">Imported {result.created} {plural}.</div>'
            f'{_import_result_table(result)}</section>'
        )

    return _page(
        title="Aphrodite Catalog Import",
        body=f"""
        <header>
          <h1>Catalog Import</h1>
          <nav><a href="/admin/jobs">Jobs</a><a href="/admin/jobs?review=needs_review">Needs review</a><a href="/v1/catalog-import/template.csv">CSV template</a></nav>
        </header>
        {banner}
        <section>
          <form method="post" action="/admin/import" enctype="multipart/form-data" class="import-form">
            <div class="form-grid">
              <label class="field">
                <span>Project</span>
                <select name="project_id" required>
                  {project_options}
                </select>
              </label>
              <label class="field">
                <span>Background</span>
                <select name="background_style">
                  {background_options}
                </select>
              </label>
              <label class="field">
                <span>Quantity per target</span>
                <input type="number" name="quantity_per_target" min="1" max="8" value="{_h(quantity_per_target)}">
              </label>
              <label class="field">
                <span>Priority</span>
                <input type="number" name="priority" min="0" max="10" value="{_h(priority)}">
              </label>
            </div>
            <label class="field">
              <span>Background prompt</span>
              <input type="text" name="background_prompt" maxlength="1000" value="{_h(background_prompt or "")}">
            </label>
            <div class="field">
              <span>Marketplace targets</span>
              <div class="checkbox-grid">{target_options}</div>
            </div>
            <label class="field">
              <span>CSV file</span>
              <input type="file" name="file" accept=".csv,text/csv" required>
            </label>
            <div class="actions-row">
              <button type="submit">Import CSV</button>
              <a class="button" href="/v1/catalog-import/template.csv">Download template</a>
            </div>
          </form>
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
            <div><dt>Client</dt><dd>{_client_detail(job)}</dd></div>
            <div><dt>Project</dt><dd>{_project_detail(job)}</dd></div>
            <div><dt>Worker</dt><dd>{_h(job.claimed_by or "-")}</dd></div>
            <div><dt>Claim expires</dt><dd>{_h(job.claim_expires_at or "-")}</dd></div>
            <div><dt>Failure</dt><dd>{_failure_category_label(job.failure_category)}</dd></div>
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


def _job_row(job: JobRecord, *, spend: XAISpendSummary) -> str:
    spend_usd = sum(entry.cost_usd for entry in spend.entries if entry.job_id == job.id)
    review = _review_summary(job)
    ownership = _ownership_summary(job)
    return f"""
    <tr>
      <td><a href="/admin/jobs/{_u(job.id)}">{_h(job.product.name)}</a><small>{_h(job.id)}</small></td>
      <td><span class="status status-{_h(job.status.value)}">{_h(job.status.value)}</span></td>
      <td>{ownership}</td>
      <td>{len(job.outputs)} / {len(job.output_plan)}</td>
      <td>{review}</td>
      <td>{_h(job.claimed_by or "-")}</td>
      <td>{_h(job.updated_at)}</td>
      <td>${spend_usd:.4f}</td>
    </tr>
    """


def _project_admin_banner(project: ProjectRecord) -> str:
    return f"""
    <section>
      <div class="alert">
        <strong>{_h(project.name)}</strong>
        <small>{_project_client_label(project)}</small>
        <div class="actions-row">
          <a class="button" href="/admin/projects/{_u(project.id)}">Open project dashboard</a>
          <a class="button" href="/admin/import?project_id={_u(project.id)}">Import CSV</a>
        </div>
      </div>
    </section>
    """


def _ownership_summary(job: JobRecord) -> str:
    if job.project is None:
        return '<span class="muted">Unassigned</span>'
    client_label = job.project.client.name if job.project.client is not None else job.project.client_id
    return (
        f'<a href="/admin/jobs?client_id={_u(job.project.client_id)}">{_h(client_label)}</a>'
        f'<small><a href="/admin/projects/{_u(job.project.id)}">{_h(job.project.name)}</a></small>'
    )


def _client_detail(job: JobRecord) -> str:
    if job.project is None:
        return "-"
    client_label = job.project.client.name if job.project.client is not None else job.project.client_id
    return f'<a href="/admin/jobs?client_id={_u(job.project.client_id)}">{_h(client_label)}</a>'


def _project_detail(job: JobRecord) -> str:
    if job.project is None:
        return "-"
    return f'<a href="/admin/projects/{_u(job.project.id)}">{_h(job.project.name)}</a>'


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
        '<a href="/admin/import">Import CSV</a>',
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


def _project_option(project: ProjectRecord, *, selected: bool) -> str:
    client = project.client.name if project.client is not None else project.client_id
    selected_attr = " selected" if selected else ""
    label = f"{client} / {project.name}"
    return f'<option value="{_h(project.id)}"{selected_attr}>{_h(label)}</option>'


def _marketplace_checkbox(spec: MarketplaceSpec, *, checked: bool) -> str:
    checked_attr = " checked" if checked else ""
    return f"""
    <label>
      <input type="checkbox" name="marketplace_targets" value="{_h(spec.id)}"{checked_attr}>
      <span>{_h(spec.label)}</span>
      <small>{_h(spec.id)}</small>
    </label>
    """


def _option(value: str, label: str, *, selected: bool) -> str:
    selected_attr = " selected" if selected else ""
    return f'<option value="{_h(value)}"{selected_attr}>{_h(label)}</option>'


def _import_result_table(result: ProjectJobBatchRecord) -> str:
    rows = "\n".join(
        f"""
        <tr>
          <td><a href="/admin/jobs/{_u(job.id)}">{_h(job.product.name)}</a><small>{_h(job.id)}</small></td>
          <td>{_h(job.product.sku or "-")}</td>
          <td>{", ".join(_h(target) for target in job.marketplace_targets)}</td>
          <td>{_h(str(job.priority))}</td>
          <td><span class="status status-{_h(job.status.value)}">{_h(job.status.value)}</span></td>
        </tr>
        """
        for job in result.jobs
    )
    links = [
        f'<a class="button" href="/admin/projects/{_u(result.project_id)}">Open project dashboard</a>',
        f'<a class="button" href="/admin/projects/{_u(result.project_id)}/batches/{_u(result.id)}">Open import batch</a>',
    ]
    project_link = f'<p class="actions-row">{"".join(links)}</p>'
    return f"""
    {project_link}
    <table>
      <thead><tr><th>Product</th><th>SKU</th><th>Targets</th><th>Priority</th><th>Status</th></tr></thead>
      <tbody>{rows}</tbody>
    </table>
    """


def _project_client_label(project: ProjectRecord) -> str:
    return project.client.name if project.client is not None else project.client_id


def _project_client_link(project: ProjectRecord) -> str:
    return f'<a href="/admin/jobs?client_id={_u(project.client_id)}">{_h(_project_client_label(project))}</a>'


def _project_metrics(jobs: list[JobRecord]) -> str:
    job_counts = {status: sum(1 for job in jobs if job.status == status) for status in JobStatus}
    outputs = [output for job in jobs for output in job.outputs]
    pending = sum(1 for output in outputs if output.review_status == OutputReviewStatus.PENDING_REVIEW)
    approved = sum(1 for output in outputs if output.review_status == OutputReviewStatus.APPROVED)
    rejected = sum(1 for output in outputs if output.review_status == OutputReviewStatus.REJECTED)
    return f"""
    <section class="metrics metrics-wide">
      <div><span>Jobs</span><strong>{len(jobs)}</strong></div>
      <div><span>Queued</span><strong>{job_counts[JobStatus.QUEUED]}</strong></div>
      <div><span>Rendering</span><strong>{job_counts[JobStatus.RENDERING]}</strong></div>
      <div><span>Completed</span><strong>{job_counts[JobStatus.COMPLETED]}</strong></div>
      <div><span>Failed</span><strong>{job_counts[JobStatus.FAILED]}</strong></div>
      <div><span>Outputs</span><strong>{len(outputs)}</strong></div>
      <div><span>Pending</span><strong>{pending}</strong></div>
      <div><span>Approved</span><strong>{approved}</strong></div>
      <div><span>Rejected</span><strong>{rejected}</strong></div>
    </section>
    """


def _project_review_nav(
    *,
    project: ProjectRecord,
    review_filter: OutputReviewStatus | None,
) -> str:
    items = [
        ("All", None),
        ("Pending", OutputReviewStatus.PENDING_REVIEW),
        ("Approved", OutputReviewStatus.APPROVED),
        ("Rejected", OutputReviewStatus.REJECTED),
    ]
    links = []
    for label, value in items:
        href = f"/admin/projects/{_u(project.id)}"
        if value is not None:
            href += f"?review={_u(value.value)}"
        selected = " selected" if value == review_filter else ""
        if value is None and review_filter is None:
            selected = " selected"
        links.append(f'<a class="filter-link{selected}" href="{href}">{_h(label)}</a>')
    return f'<nav class="filter-nav">{"".join(links)}</nav>'


def _project_review_outputs(
    *,
    jobs: list[JobRecord],
    review_filter: OutputReviewStatus | None,
) -> list[tuple[JobRecord, JobOutputRecord]]:
    rows = [(job, output) for job in jobs for output in job.outputs]
    if review_filter is None:
        return rows
    return [(job, output) for job, output in rows if output.review_status == review_filter]


def _project_approved_output_count(jobs: list[JobRecord]) -> int:
    return sum(
        1
        for job in jobs
        for output in job.outputs
        if output.review_status == OutputReviewStatus.APPROVED
    )


def _project_pending_output_count(jobs: list[JobRecord]) -> int:
    return sum(
        1
        for job in jobs
        for output in job.outputs
        if output.review_status == OutputReviewStatus.PENDING_REVIEW
    )


def _project_bulk_review_controls(*, project: ProjectRecord, jobs: list[JobRecord]) -> str:
    pending = _project_pending_output_count(jobs)
    if pending == 0:
        return '<div class="alert bulk-review"><strong>No pending outputs.</strong></div>'

    plural = "output" if pending == 1 else "outputs"
    return f"""
    <div class="alert bulk-review">
      <strong>{pending} pending {plural}</strong>
      <div class="actions-row">
        <form method="post" action="/admin/projects/{_u(project.id)}/outputs/approve-pending">
          <button type="submit">Approve pending</button>
        </form>
        <form method="post" action="/admin/projects/{_u(project.id)}/outputs/reject-pending">
          <textarea
            class="compact"
            name="note"
            maxlength="2000"
            placeholder="Reason"
          ></textarea>
          <button type="submit" class="danger">Reject pending</button>
        </form>
      </div>
    </div>
    """


def _project_batch_history(
    *,
    project: ProjectRecord,
    batches: list[ProjectJobBatchRecord],
    jobs: list[JobRecord],
    spend: XAISpendSummary,
) -> str:
    project_retry = _project_retry_action(project=project, jobs=jobs)
    if not batches:
        return project_retry + '<p class="muted">No saved imports yet.</p>'

    rows = "\n".join(
        _project_batch_row(project=project, batch=batch, spend=spend) for batch in batches
    )
    return f"""
    {project_retry}
    <table>
      <thead><tr><th>Batch</th><th>Source</th><th>Jobs</th><th>Outputs</th><th>Approval</th><th>Spend</th><th>Status</th><th>Alerts</th><th>Updated</th><th>Actions</th></tr></thead>
      <tbody>{rows}</tbody>
    </table>
    """


def _project_retry_action(*, project: ProjectRecord, jobs: list[JobRecord]) -> str:
    failed = sum(1 for job in jobs if job.status == JobStatus.FAILED)
    if failed == 0:
        return ""
    plural = "job" if failed == 1 else "jobs"
    return f"""
    <div class="alert bulk-review">
      <strong>{failed} failed project {plural}</strong>
      <form method="post" action="/admin/projects/{_u(project.id)}/jobs/retry-failed">
        <button type="submit">Retry failed project jobs</button>
      </form>
    </div>
    """


def _project_batch_row(
    *,
    project: ProjectRecord,
    batch: ProjectJobBatchRecord,
    spend: XAISpendSummary,
) -> str:
    report = project_job_batch_report(batch=batch, spend=spend)
    counts = _job_status_counts(batch.jobs)
    return f"""
    <tr>
      <td><a href="/admin/projects/{_u(project.id)}/batches/{_u(batch.id)}">{_h(batch.id[:8])}</a><small>{_h(batch.id)}</small></td>
      <td>{_h(_batch_source_label(batch.source))}</td>
      <td>{report.job_count}</td>
      <td>{report.output_count} / {report.planned_output_count}</td>
      <td>{_format_percent(report.approval_rate)}</td>
      <td>${report.xai_cost_usd:.4f}</td>
      <td>{_format_status_counts(counts)}</td>
      <td>{_batch_alert_summary(report)}</td>
      <td>{_h(report.last_updated_at or batch.created_at)}</td>
      <td>{_batch_retry_action(project=project, batch=batch)}</td>
    </tr>
    """


def _batch_alert_summary(report) -> str:
    if not report.alerts:
        return '<span class="muted">None</span>'
    critical = sum(1 for alert in report.alerts if alert.level == "critical")
    warnings = sum(1 for alert in report.alerts if alert.level == "warning")
    parts = []
    if critical:
        parts.append(f'<span class="status status-failed">{critical} critical</span>')
    if warnings:
        parts.append(f'<span class="status status-queued">{warnings} warning</span>')
    return " ".join(parts)


def _batch_alert_panel(
    *,
    project: ProjectRecord,
    batch: ProjectJobBatchRecord,
    report,
    alert_records: list[ProjectJobBatchAlertRecord],
) -> str:
    if alert_records:
        return "\n".join(
            _batch_alert_record_panel(project=project, batch=batch, alert=alert)
            for alert in alert_records
        )
    if not report.alerts:
        return '<p class="muted">No batch alerts.</p>'
    return "\n".join(
        f"""
        <div class="alert alert-{_h(alert.level)}">
          <strong>{_h(alert.code.replace("_", " "))}</strong>
          <small>{_h(alert.message)}</small>
        </div>
        """
        for alert in report.alerts
    )


def _batch_alert_record_panel(
    *,
    project: ProjectRecord,
    batch: ProjectJobBatchRecord,
    alert: ProjectJobBatchAlertRecord,
) -> str:
    return f"""
    <div class="alert alert-{_h(alert.level)}">
      <strong>{_h(alert.code.replace("_", " "))}</strong>
      <small>{_h(alert.message)}</small>
      {_batch_alert_record_status(alert)}
      <div class="actions-row">
        <form method="post" action="/admin/projects/{_u(project.id)}/batches/{_u(batch.id)}/alerts/{_u(alert.id)}/acknowledge">
          <button type="submit">Acknowledge</button>
        </form>
        <form method="post" action="/admin/projects/{_u(project.id)}/batches/{_u(batch.id)}/alerts/{_u(alert.id)}/mute">
          <input type="number" name="hours" min="1" max="720" value="24" aria-label="Mute hours">
          <button type="submit">Mute</button>
        </form>
      </div>
    </div>
    """


def _batch_alert_record_status(alert: ProjectJobBatchAlertRecord) -> str:
    states = []
    if alert.resolved_at:
        states.append(f"Resolved at {alert.resolved_at}")
    if alert.acknowledged_at:
        states.append(f"Acknowledged by {alert.acknowledged_by or 'operator'} at {alert.acknowledged_at}")
    if alert.muted_until:
        states.append(f"Muted until {alert.muted_until}")
    if alert.delivered_at:
        states.append(f"Delivered at {alert.delivered_at}")
    elif alert.delivery_error:
        states.append(f"Delivery error: {alert.delivery_error}")
    elif alert.delivery_attempted_at:
        states.append(f"Delivery attempted at {alert.delivery_attempted_at}")
    else:
        states.append("Not delivered")
    return f'<small>{" | ".join(_h(state) for state in states)}</small>'


def _batch_report_links(*, project: ProjectRecord, batch: ProjectJobBatchRecord) -> str:
    return (
        f'<a href="/v1/projects/{_u(project.id)}/jobs/batches/{_u(batch.id)}/report.json">Report JSON</a>'
        f'<a href="/v1/projects/{_u(project.id)}/jobs/batches/{_u(batch.id)}/report.csv">Report CSV</a>'
    )


def _batch_retry_action(*, project: ProjectRecord, batch: ProjectJobBatchRecord) -> str:
    failed = sum(1 for job in batch.jobs if job.status == JobStatus.FAILED)
    if failed == 0:
        return '<span class="muted">No failed jobs</span>'
    plural = "job" if failed == 1 else "jobs"
    return f"""
    <form method="post" action="/admin/projects/{_u(project.id)}/batches/{_u(batch.id)}/retry-failed">
      <button type="submit">Retry {failed} failed {plural}</button>
    </form>
    """


def _job_status_counts(jobs: list[JobRecord]) -> dict[JobStatus, int]:
    return {status: sum(1 for job in jobs if job.status == status) for status in JobStatus}


def _format_status_counts(counts: dict[JobStatus, int]) -> str:
    chips = []
    for status_value in [
        JobStatus.QUEUED,
        JobStatus.RENDERING,
        JobStatus.COMPLETED,
        JobStatus.FAILED,
        JobStatus.CANCELED,
    ]:
        count = counts[status_value]
        if count:
            chips.append(
                f'<span class="status status-{_h(status_value.value)}">{count} {_h(status_value.value)}</span>'
            )
    return " ".join(chips) if chips else '<span class="muted">No jobs</span>'


def _batch_source_label(source: str) -> str:
    return source.replace("_", " ")


def _failure_category_label(category: JobFailureCategory | None) -> str:
    if category is None:
        return "-"
    return category.value.replace("_", " ").title()


def _format_percent(value: float) -> str:
    return f"{value * 100:.0f}%"


def _batch_job_row(job: JobRecord) -> str:
    return f"""
    <tr>
      <td><a href="/admin/jobs/{_u(job.id)}">{_h(job.product.name)}</a><small>{_h(job.product.sku or job.id)}</small></td>
      <td><span class="status status-{_h(job.status.value)}">{_h(job.status.value)}</span></td>
      <td>{_failure_category_label(job.failure_category)}</td>
      <td>{len(job.outputs)} / {len(job.output_plan)}</td>
      <td>{_review_summary(job)}</td>
      <td>{job.priority}</td>
      <td>{_h(job.updated_at)}</td>
      <td>{_h(job.error or "-")}</td>
    </tr>
    """


def _project_spend_usd(*, jobs: list[JobRecord], spend: XAISpendSummary) -> float:
    job_ids = {job.id for job in jobs}
    return sum(entry.cost_usd for entry in spend.entries if entry.job_id in job_ids)


def _project_output_row(
    *,
    project: ProjectRecord,
    job: JobRecord,
    output: JobOutputRecord,
    review_filter: OutputReviewStatus | None,
) -> str:
    filter_query = f"?review={_u(review_filter.value)}" if review_filter is not None else ""
    src = f"/admin/jobs/{_u(job.id)}/outputs/{_u(output.variant_id)}/file"
    export = ""
    if output.review_status == OutputReviewStatus.APPROVED:
        export = f'<a class="button" href="/admin/jobs/{_u(job.id)}/outputs/{_u(output.variant_id)}/export">Export</a>'
    note = _h(output.review_note or "-")
    return f"""
    <tr>
      <td><a href="/admin/jobs/{_u(job.id)}">{_h(job.product.name)}</a><small>{_h(job.product.sku or job.id)}</small></td>
      <td>{_h(output.variant_id)}</td>
      <td><span class="review review-{_h(output.review_status.value)}">{_h(output.review_status.value.replace("_", " "))}</span></td>
      <td><a href="{src}">{_h(Path(output.storage_path).name)}</a></td>
      <td>{output.width}x{output.height}<small>{output.bytes} bytes</small></td>
      <td>{_h(output.updated_at)}</td>
      <td>{note}</td>
      <td>
        <div class="actions-inline">
          <form method="post" action="/admin/projects/{_u(project.id)}/jobs/{_u(job.id)}/outputs/{_u(output.variant_id)}/approve{filter_query}">
            <button type="submit">Approve</button>
          </form>
          <form method="post" action="/admin/projects/{_u(project.id)}/jobs/{_u(job.id)}/outputs/{_u(output.variant_id)}/reject{filter_query}">
            <textarea class="compact" name="note" maxlength="2000" placeholder="Reason"></textarea>
            <button type="submit" class="danger">Reject</button>
          </form>
          {export}
        </div>
      </td>
    </tr>
    """


def _project_job_row(job: JobRecord) -> str:
    return f"""
    <tr>
      <td><a href="/admin/jobs/{_u(job.id)}">{_h(job.product.name)}</a><small>{_h(job.id)}</small></td>
      <td><span class="status status-{_h(job.status.value)}">{_h(job.status.value)}</span></td>
      <td>{_failure_category_label(job.failure_category)}</td>
      <td>{len(job.outputs)} / {len(job.output_plan)}</td>
      <td>{_review_summary(job)}</td>
      <td>{job.priority}</td>
      <td>{_h(job.updated_at)}</td>
    </tr>
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
          .metrics-wide {{ grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); }}
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
          .actions-row {{ display: flex; gap: 10px; flex-wrap: wrap; margin-top: 10px; }}
          .actions-inline {{ display: flex; gap: 8px; align-items: flex-start; flex-wrap: wrap; }}
          .actions-inline form {{ display: flex; gap: 6px; }}
          .import-form {{ background: var(--panel); border: 1px solid var(--line); padding: 16px; }}
          .bulk-review {{ margin: 0 0 12px; }}
          .bulk-review form {{ display: flex; gap: 8px; align-items: flex-start; flex-wrap: wrap; }}
          .form-grid {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; }}
          .field {{ display: grid; gap: 6px; }}
          .field > span {{ color: var(--muted); font-size: 12px; text-transform: uppercase; }}
          .checkbox-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 8px; }}
          .checkbox-grid label {{ display: grid; grid-template-columns: auto 1fr; column-gap: 8px; align-items: start; border: 1px solid var(--line); padding: 8px; }}
          .checkbox-grid small {{ grid-column: 2; }}
          .filter-nav {{ margin: 0 0 12px; }}
          .filter-link {{ display: inline-block; border: 1px solid var(--line); padding: 5px 9px; }}
          .filter-link.selected {{ background: #eef7f5; color: var(--accent); }}
          .alert {{ background: var(--panel); border: 1px solid var(--line); padding: 12px; }}
          .alert-error, .alert-critical {{ border-color: #f0b4ae; color: var(--bad); background: #fff7f6; }}
          .alert-warning {{ border-color: #e6cf8a; color: var(--warn); background: #fffaf0; }}
          .alert-success {{ border-color: #9ed7ca; color: var(--accent); background: #eef7f5; }}
          form {{ display: grid; gap: 12px; margin: 0; }}
          input, select, textarea {{ width: 100%; border: 1px solid var(--line); padding: 8px; font: inherit; background: #fff; color: var(--ink); }}
          input[type="checkbox"] {{ width: auto; margin-top: 3px; }}
          textarea {{ min-height: 56px; resize: vertical; }}
          textarea.compact {{ width: 160px; min-height: 34px; }}
          button, .button {{ display: inline-block; width: fit-content; border: 1px solid var(--line); background: #eef7f5; color: var(--accent); padding: 6px 10px; font: inherit; cursor: pointer; text-decoration: none; }}
          button:hover, .button:hover {{ text-decoration: none; filter: brightness(0.97); }}
          .danger {{ background: #fff1f0; color: var(--bad); }}
          @media (max-width: 760px) {{
            header {{ align-items: flex-start; flex-direction: column; }}
            .metrics, .summary dl, .media-grid dl, .form-grid {{ grid-template-columns: 1fr; }}
            table {{ display: block; overflow-x: auto; }}
          }}
        </style>
      </head>
      <body>{body}</body>
    </html>
    """



def _h(value: object) -> str:
    return escape(str(value), quote=True)


def _u(value: object) -> str:
    return quote(str(value), safe="")
