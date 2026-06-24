"""HTTP API for Aphrodite."""

from __future__ import annotations

import logging
import re
import secrets
import uuid
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Annotated

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse, HTMLResponse, Response
from pydantic import ValidationError

from aphrodite import __version__
from aphrodite.admin import (
    render_admin_catalog_import,
    render_admin_job_detail,
    render_admin_jobs_index,
    render_admin_project_batch_detail,
    render_admin_project_detail,
)
from aphrodite.alerts import process_project_job_batch_alerts
from aphrodite.assets import (
    AssetStorageError,
    AssetValidationError,
    storage_path_for,
    validate_image_upload,
    write_asset_file,
)
from aphrodite.catalog_import import (
    CatalogImportError,
    catalog_csv_template,
    parse_catalog_csv,
    split_marketplace_targets,
)
from aphrodite.config import Settings
from aphrodite.domain import (
    AssetRecord,
    BackgroundIntent,
    ClientCreate,
    ClientRecord,
    JobCreate,
    JobFailureRequest,
    JobOutputCreate,
    JobOutputRecord,
    JobRecord,
    JobStatus,
    JobStatusUpdate,
    OutputReviewStatus,
    ProjectCreate,
    ProjectJobBatchCreate,
    ProjectJobBatchRecord,
    ProjectJobBatchReport,
    ProjectRecord,
    WorkerClaimRefreshRequest,
    WorkerClaimRequest,
    WorkerJobClaim,
)
from aphrodite.marketplaces import list_marketplace_specs
from aphrodite.reporting import (
    XAISpendSummary,
    project_job_batch_report,
    project_job_batch_report_csv,
    read_xai_spend_summary,
    xai_cost_ledger_path,
)
from aphrodite.storage import OutputStorageError, resolve_existing_media_file
from aphrodite.store import (
    AssetNotFoundError,
    ClientNotFoundError,
    JobStore,
    OutputVariantNotFoundError,
    ProjectNotFoundError,
)

LOG = logging.getLogger(__name__)


def create_app(settings: Settings | None = None, store: JobStore | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    Path(settings.media_root).mkdir(parents=True, exist_ok=True)
    store = store or JobStore(settings.db_path)
    store.initialize()

    app = FastAPI(
        title="Aphrodite",
        version=__version__,
        description="AI product photography jobs for e-commerce product imagery.",
    )
    app.state.settings = settings
    app.state.store = store

    def require_api_auth(
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> None:
        _require_bearer_token(authorization=authorization, expected_token=settings.api_token)

    def require_worker_auth(
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> None:
        expected_token = settings.worker_token or settings.api_token
        _require_bearer_token(authorization=authorization, expected_token=expected_token)

    def create_project_job_batch_record(
        *,
        project_id: str,
        payload: ProjectJobBatchCreate,
        source: str = "api",
    ) -> ProjectJobBatchRecord:
        jobs = create_project_job_batch_jobs(project_id=project_id, payload=payload, source=source)
        batch_id = jobs[0].batch_id if jobs else None
        batch = store.get_project_job_batch(batch_id) if batch_id is not None else None
        if batch is None:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="failed to load created batch",
            )
        return batch

    def create_project_job_batch_jobs(
        *,
        project_id: str,
        payload: ProjectJobBatchCreate,
        source: str = "api",
    ) -> list[JobRecord]:
        try:
            return store.create_project_job_batch(
                project_id=project_id,
                request=payload,
                source=source,
            )
        except ProjectNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"project not found: {exc.project_id}",
            ) from exc
        except AssetNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"source asset not found: {exc.asset_id}",
            ) from exc

    def project_or_404(project_id: str) -> ProjectRecord:
        project = store.get_project(project_id)
        if project is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="project not found")
        return project

    def project_job_or_404(project_id: str, job_id: str) -> JobRecord:
        job = store.get_job(job_id)
        if job is None or job.project_id != project_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="project job not found",
            )
        return job

    def project_batch_or_404(project_id: str, batch_id: str) -> ProjectJobBatchRecord:
        batch = store.get_project_job_batch(batch_id)
        if batch is None or batch.project_id != project_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="project batch not found",
            )
        return batch

    def process_project_batch_alerts(*, project_id: str, batch_id: str) -> None:
        try:
            process_project_job_batch_alerts(
                store=store,
                settings=settings,
                project_id=project_id,
                batch_id=batch_id,
            )
        except Exception:
            LOG.exception(
                "failed to process project job batch alerts",
                extra={"project_id": project_id, "batch_id": batch_id},
            )

    def process_job_batch_alerts(job: JobRecord) -> None:
        if job.project_id is None or job.batch_id is None:
            return
        process_project_batch_alerts(project_id=job.project_id, batch_id=job.batch_id)

    def project_pending_outputs(project_id: str) -> list[tuple[JobRecord, JobOutputRecord]]:
        jobs = store.list_jobs(project_id=project_id, limit=100)
        return [
            (job, output)
            for job in jobs
            for output in job.outputs
            if output.review_status == OutputReviewStatus.PENDING_REVIEW
        ]

    def admin_project_page(
        *,
        project_id: str,
        review_filter: OutputReviewStatus | None = None,
        status_code: int = status.HTTP_200_OK,
        message: str | None = None,
    ) -> HTMLResponse:
        project = project_or_404(project_id)
        jobs = store.list_jobs(project_id=project_id, limit=100)
        batches = store.list_project_job_batches(project_id=project_id, limit=20)
        spend = _xai_spend_summary(settings.media_root)
        return HTMLResponse(
            render_admin_project_detail(
                project=project,
                jobs=jobs,
                spend=spend,
                batches=batches,
                review_filter=review_filter,
                message=message,
            ),
            status_code=status_code,
        )

    def admin_project_batch_page(
        *,
        project_id: str,
        batch_id: str,
        message: str | None = None,
        status_code: int = status.HTTP_200_OK,
    ) -> HTMLResponse:
        project = project_or_404(project_id)
        batch = project_batch_or_404(project_id, batch_id)
        spend = _xai_spend_summary(settings.media_root)
        alert_records = store.list_project_job_batch_alerts(
            project_id=project_id,
            batch_id=batch_id,
        )
        return HTMLResponse(
            render_admin_project_batch_detail(
                project=project,
                batch=batch,
                spend=spend,
                alert_records=alert_records,
                message=message,
            ),
            status_code=status_code,
        )

    def admin_import_page(
        *,
        selected_project_id: str | None = None,
        selected_targets: list[str] | None = None,
        background_style: str = "clean_white",
        background_prompt: str | None = None,
        quantity_per_target: int = 1,
        priority: int = 5,
        result: ProjectJobBatchRecord | None = None,
        error: str | None = None,
        status_code: int = status.HTTP_200_OK,
    ) -> HTMLResponse:
        return HTMLResponse(
            render_admin_catalog_import(
                projects=store.list_projects(limit=100),
                marketplace_specs=list_marketplace_specs(),
                selected_project_id=selected_project_id,
                selected_targets=selected_targets,
                background_style=background_style,
                background_prompt=background_prompt,
                quantity_per_target=quantity_per_target,
                priority=priority,
                result=result,
                error=error,
            ),
            status_code=status_code,
        )

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {
            "status": "ok",
            "service": settings.service_name,
            "version": __version__,
            "env": settings.env,
        }

    @app.get("/readiness")
    def readiness() -> dict[str, str]:
        if not store.ping():
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="job store is unavailable",
            )
        Path(settings.media_root).mkdir(parents=True, exist_ok=True)
        return {"status": "ready", "store": "sqlite"}

    @app.get(
        "/admin",
        response_class=HTMLResponse,
        dependencies=[Depends(require_api_auth)],
    )
    def admin_home() -> HTMLResponse:
        return admin_jobs()

    @app.get(
        "/admin/jobs",
        response_class=HTMLResponse,
        dependencies=[Depends(require_api_auth)],
    )
    def admin_jobs(
        review: Annotated[str | None, Query(pattern="^needs_review$")] = None,
        client_id: Annotated[str | None, Query(min_length=1, max_length=80)] = None,
        project_id: Annotated[str | None, Query(min_length=1, max_length=80)] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> HTMLResponse:
        active_project = project_or_404(project_id) if project_id is not None else None
        jobs = store.list_jobs(client_id=client_id, project_id=project_id, limit=limit)
        if review == "needs_review":
            jobs = [job for job in jobs if _job_needs_review(job)]
        spend = _xai_spend_summary(settings.media_root)
        return HTMLResponse(
            render_admin_jobs_index(jobs=jobs, spend=spend, active_project=active_project)
        )

    @app.get(
        "/admin/import",
        response_class=HTMLResponse,
        dependencies=[Depends(require_api_auth)],
    )
    def admin_catalog_import(
        project_id: Annotated[str | None, Query(min_length=1, max_length=80)] = None,
    ) -> HTMLResponse:
        return admin_import_page(selected_project_id=project_id)

    @app.post(
        "/admin/import",
        response_class=HTMLResponse,
        dependencies=[Depends(require_api_auth)],
    )
    async def admin_import_catalog_csv(
        project_id: Annotated[str, Form(min_length=1, max_length=80)],
        file: Annotated[UploadFile, File(...)],
        marketplace_targets: Annotated[list[str] | None, Form()] = None,
        background_style: Annotated[str, Form(max_length=80)] = "clean_white",
        background_prompt: Annotated[str | None, Form(max_length=1000)] = None,
        quantity_per_target: Annotated[int, Form(ge=1, le=8)] = 1,
        priority: Annotated[int, Form(ge=0, le=10)] = 5,
    ) -> HTMLResponse:
        selected_targets = marketplace_targets or []
        if not selected_targets:
            return admin_import_page(
                selected_project_id=project_id,
                selected_targets=selected_targets,
                background_style=background_style,
                background_prompt=background_prompt,
                quantity_per_target=quantity_per_target,
                priority=priority,
                error="Select at least one marketplace target.",
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            )
        content = await file.read(settings.max_upload_bytes + 1)
        if len(content) > settings.max_upload_bytes:
            return admin_import_page(
                selected_project_id=project_id,
                selected_targets=selected_targets,
                background_style=background_style,
                background_prompt=background_prompt,
                quantity_per_target=quantity_per_target,
                priority=priority,
                error="CSV upload is too large.",
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            )
        try:
            payload = parse_catalog_csv(
                content,
                marketplace_targets=selected_targets,
                background=BackgroundIntent(style=background_style, prompt=background_prompt),
                quantity_per_target=quantity_per_target,
                priority=priority,
            )
            result = create_project_job_batch_record(
                project_id=project_id,
                payload=payload,
                source="admin_csv",
            )
        except CatalogImportError as exc:
            return admin_import_page(
                selected_project_id=project_id,
                selected_targets=selected_targets,
                background_style=background_style,
                background_prompt=background_prompt,
                quantity_per_target=quantity_per_target,
                priority=priority,
                error=exc.message,
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            )
        except ValidationError as exc:
            return admin_import_page(
                selected_project_id=project_id,
                selected_targets=selected_targets,
                background_style=background_style,
                background_prompt=background_prompt,
                quantity_per_target=quantity_per_target,
                priority=priority,
                error=str(exc.errors()[0].get("msg", "invalid import defaults")),
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            )
        except HTTPException as exc:
            return admin_import_page(
                selected_project_id=project_id,
                selected_targets=selected_targets,
                background_style=background_style,
                background_prompt=background_prompt,
                quantity_per_target=quantity_per_target,
                priority=priority,
                error=str(exc.detail),
                status_code=exc.status_code,
            )
        return admin_import_page(
            selected_project_id=project_id,
            selected_targets=selected_targets,
            background_style=background_style,
            background_prompt=background_prompt,
            quantity_per_target=quantity_per_target,
            priority=priority,
            result=result,
        )

    @app.get(
        "/admin/projects/{project_id}",
        response_class=HTMLResponse,
        dependencies=[Depends(require_api_auth)],
    )
    def admin_project_detail(
        project_id: str,
        review: OutputReviewStatus | None = None,
    ) -> HTMLResponse:
        return admin_project_page(project_id=project_id, review_filter=review)

    @app.post(
        "/admin/projects/{project_id}/jobs/retry-failed",
        response_class=HTMLResponse,
        dependencies=[Depends(require_api_auth)],
    )
    def admin_project_retry_failed_jobs(project_id: str) -> HTMLResponse:
        project_or_404(project_id)
        retried = store.retry_failed_jobs(project_id=project_id)
        for batch in store.list_project_job_batches(project_id=project_id, limit=100):
            process_project_batch_alerts(project_id=project_id, batch_id=batch.id)
        plural = "job" if retried == 1 else "jobs"
        return admin_project_page(
            project_id=project_id,
            message=f"Requeued {retried} failed {plural}.",
        )

    @app.get(
        "/admin/projects/{project_id}/batches/{batch_id}",
        response_class=HTMLResponse,
        dependencies=[Depends(require_api_auth)],
    )
    def admin_project_batch_detail(project_id: str, batch_id: str) -> HTMLResponse:
        return admin_project_batch_page(project_id=project_id, batch_id=batch_id)

    @app.post(
        "/admin/projects/{project_id}/batches/{batch_id}/alerts/{alert_id}/acknowledge",
        response_class=HTMLResponse,
        dependencies=[Depends(require_api_auth)],
    )
    def admin_acknowledge_batch_alert(
        project_id: str,
        batch_id: str,
        alert_id: str,
    ) -> HTMLResponse:
        project_batch_or_404(project_id, batch_id)
        record = store.acknowledge_project_job_batch_alert(
            project_id=project_id,
            batch_id=batch_id,
            alert_id=alert_id,
            acknowledged_by="operator",
        )
        if record is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="alert not found")
        return admin_project_batch_page(
            project_id=project_id,
            batch_id=batch_id,
            message="Acknowledged alert.",
        )

    @app.post(
        "/admin/projects/{project_id}/batches/{batch_id}/alerts/{alert_id}/mute",
        response_class=HTMLResponse,
        dependencies=[Depends(require_api_auth)],
    )
    def admin_mute_batch_alert(
        project_id: str,
        batch_id: str,
        alert_id: str,
        hours: Annotated[int, Form(ge=1, le=720)] = 24,
    ) -> HTMLResponse:
        project_batch_or_404(project_id, batch_id)
        record = store.mute_project_job_batch_alert(
            project_id=project_id,
            batch_id=batch_id,
            alert_id=alert_id,
            hours=hours,
        )
        if record is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="alert not found")
        return admin_project_batch_page(
            project_id=project_id,
            batch_id=batch_id,
            message="Muted alert.",
        )

    @app.post(
        "/admin/projects/{project_id}/batches/{batch_id}/retry-failed",
        response_class=HTMLResponse,
        dependencies=[Depends(require_api_auth)],
    )
    def admin_project_batch_retry_failed_jobs(project_id: str, batch_id: str) -> HTMLResponse:
        project_batch_or_404(project_id, batch_id)
        retried = store.retry_failed_jobs(project_id=project_id, batch_id=batch_id)
        process_project_batch_alerts(project_id=project_id, batch_id=batch_id)
        plural = "job" if retried == 1 else "jobs"
        return admin_project_batch_page(
            project_id=project_id,
            batch_id=batch_id,
            message=f"Requeued {retried} failed {plural}.",
        )

    @app.post(
        "/admin/projects/{project_id}/outputs/approve-pending",
        response_class=HTMLResponse,
        dependencies=[Depends(require_api_auth)],
    )
    def admin_project_approve_pending_outputs(project_id: str) -> HTMLResponse:
        project_or_404(project_id)
        pending = project_pending_outputs(project_id)
        reviewed = 0
        for job, output in pending:
            if (
                store.review_output(
                    job_id=job.id,
                    variant_id=output.variant_id,
                    review_status=OutputReviewStatus.APPROVED,
                )
                is not None
            ):
                reviewed += 1

        plural = "output" if reviewed == 1 else "outputs"
        return admin_project_page(
            project_id=project_id,
            message=f"Approved {reviewed} pending {plural}.",
        )

    @app.post(
        "/admin/projects/{project_id}/outputs/reject-pending",
        response_class=HTMLResponse,
        dependencies=[Depends(require_api_auth)],
    )
    def admin_project_reject_pending_outputs(
        project_id: str,
        note: Annotated[str | None, Form(max_length=2000)] = None,
    ) -> HTMLResponse:
        project_or_404(project_id)
        pending = project_pending_outputs(project_id)
        reviewed = 0
        for job, output in pending:
            if (
                store.review_output(
                    job_id=job.id,
                    variant_id=output.variant_id,
                    review_status=OutputReviewStatus.REJECTED,
                    note=note,
                )
                is not None
            ):
                reviewed += 1

        plural = "output" if reviewed == 1 else "outputs"
        return admin_project_page(
            project_id=project_id,
            message=f"Rejected {reviewed} pending {plural}.",
        )

    @app.post(
        "/admin/projects/{project_id}/jobs/{job_id}/outputs/{variant_id}/approve",
        response_class=HTMLResponse,
        dependencies=[Depends(require_api_auth)],
    )
    def admin_project_approve_output(
        project_id: str,
        job_id: str,
        variant_id: str,
        review: OutputReviewStatus | None = None,
    ) -> HTMLResponse:
        project_job_or_404(project_id, job_id)
        output = store.review_output(
            job_id=job_id,
            variant_id=variant_id,
            review_status=OutputReviewStatus.APPROVED,
        )
        if output is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="output not found")
        return admin_project_page(project_id=project_id, review_filter=review)

    @app.post(
        "/admin/projects/{project_id}/jobs/{job_id}/outputs/{variant_id}/reject",
        response_class=HTMLResponse,
        dependencies=[Depends(require_api_auth)],
    )
    def admin_project_reject_output(
        project_id: str,
        job_id: str,
        variant_id: str,
        review: OutputReviewStatus | None = None,
        note: Annotated[str | None, Form(max_length=2000)] = None,
    ) -> HTMLResponse:
        project_job_or_404(project_id, job_id)
        output = store.review_output(
            job_id=job_id,
            variant_id=variant_id,
            review_status=OutputReviewStatus.REJECTED,
            note=note,
        )
        if output is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="output not found")
        return admin_project_page(project_id=project_id, review_filter=review)

    @app.get(
        "/admin/projects/{project_id}/exports.zip",
        dependencies=[Depends(require_api_auth)],
    )
    def admin_export_project_approved_outputs(project_id: str) -> Response:
        project = project_or_404(project_id)
        jobs = store.list_jobs(project_id=project_id, limit=100)
        approved = [
            (job, output)
            for job in jobs
            for output in job.outputs
            if output.review_status == OutputReviewStatus.APPROVED
        ]
        if not approved:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="no approved outputs found",
            )

        buffer = BytesIO()
        archive_names: set[str] = set()
        try:
            with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
                for job, output in approved:
                    path = resolve_existing_media_file(
                        media_root=settings.media_root,
                        relative_path=output.storage_path,
                    )
                    archive.write(
                        path,
                        arcname=_unique_archive_name(
                            _project_export_arcname(job=job, output=output),
                            used=archive_names,
                        ),
                    )
        except OutputStorageError as exc:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="media path is outside the media root",
            ) from exc
        except FileNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="approved media file not found",
            ) from exc

        filename = f"{_safe_archive_part(project.name)}-approved-outputs.zip"
        headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
        return Response(buffer.getvalue(), media_type="application/zip", headers=headers)

    @app.get(
        "/admin/jobs/{job_id}",
        response_class=HTMLResponse,
        dependencies=[Depends(require_api_auth)],
    )
    def admin_job_detail(job_id: str) -> HTMLResponse:
        job = store.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="job not found")
        spend = _xai_spend_summary(settings.media_root)
        return HTMLResponse(render_admin_job_detail(job=job, spend=spend))

    @app.get(
        "/admin/spend.json",
        dependencies=[Depends(require_api_auth)],
    )
    def admin_spend_json() -> dict:
        return _xai_spend_summary(settings.media_root).as_dict()

    @app.post(
        "/admin/jobs/{job_id}/outputs/{variant_id}/approve",
        dependencies=[Depends(require_api_auth)],
    )
    def admin_approve_output(job_id: str, variant_id: str) -> HTMLResponse:
        output = store.review_output(
            job_id=job_id,
            variant_id=variant_id,
            review_status=OutputReviewStatus.APPROVED,
        )
        if output is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="output not found")
        job = store.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="job not found")
        spend = _xai_spend_summary(settings.media_root)
        return HTMLResponse(render_admin_job_detail(job=job, spend=spend))

    @app.post(
        "/admin/jobs/{job_id}/outputs/{variant_id}/reject",
        dependencies=[Depends(require_api_auth)],
    )
    def admin_reject_output(
        job_id: str,
        variant_id: str,
        note: Annotated[str | None, Form(max_length=2000)] = None,
    ) -> HTMLResponse:
        output = store.review_output(
            job_id=job_id,
            variant_id=variant_id,
            review_status=OutputReviewStatus.REJECTED,
            note=note,
        )
        if output is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="output not found")
        job = store.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="job not found")
        spend = _xai_spend_summary(settings.media_root)
        return HTMLResponse(render_admin_job_detail(job=job, spend=spend))

    @app.get(
        "/admin/assets/{asset_id}/file",
        dependencies=[Depends(require_api_auth)],
    )
    def admin_asset_file(asset_id: str) -> FileResponse:
        asset = store.get_asset(asset_id)
        if asset is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="asset not found")
        return _media_file_response(
            media_root=settings.media_root,
            relative_path=asset.storage_path,
            media_type=asset.content_type,
        )

    @app.get(
        "/admin/jobs/{job_id}/outputs/{variant_id}/file",
        dependencies=[Depends(require_api_auth)],
    )
    def admin_output_file(job_id: str, variant_id: str) -> FileResponse:
        job = store.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="job not found")
        output = _job_output_or_404(job, variant_id)
        return _media_file_response(
            media_root=settings.media_root,
            relative_path=output.storage_path,
            media_type=output.content_type,
        )

    @app.get(
        "/admin/jobs/{job_id}/outputs/{variant_id}/export",
        dependencies=[Depends(require_api_auth)],
    )
    def admin_export_output(job_id: str, variant_id: str) -> FileResponse:
        job = store.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="job not found")
        output = _job_output_or_404(job, variant_id)
        _require_approved_output(output)
        return _media_file_response(
            media_root=settings.media_root,
            relative_path=output.storage_path,
            media_type=output.content_type,
            disposition="attachment",
        )

    @app.get(
        "/admin/jobs/{job_id}/exports.zip",
        dependencies=[Depends(require_api_auth)],
    )
    def admin_export_approved_outputs(job_id: str) -> Response:
        job = store.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="job not found")
        approved = [
            output for output in job.outputs if output.review_status == OutputReviewStatus.APPROVED
        ]
        if not approved:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="no approved outputs found",
            )

        buffer = BytesIO()
        try:
            with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
                for output in approved:
                    path = resolve_existing_media_file(
                        media_root=settings.media_root,
                        relative_path=output.storage_path,
                    )
                    archive.write(path, arcname=Path(output.storage_path).name)
        except OutputStorageError as exc:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="media path is outside the media root",
            ) from exc
        except FileNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="approved media file not found",
            ) from exc

        headers = {"Content-Disposition": f'attachment; filename="{job.id}-approved-outputs.zip"'}
        return Response(buffer.getvalue(), media_type="application/zip", headers=headers)

    @app.post(
        "/v1/clients",
        response_model=ClientRecord,
        status_code=status.HTTP_201_CREATED,
        dependencies=[Depends(require_api_auth)],
    )
    def create_client(payload: ClientCreate) -> ClientRecord:
        return store.create_client(payload)

    @app.get("/v1/clients", response_model=list[ClientRecord])
    def list_clients(limit: Annotated[int, Query(ge=1, le=100)] = 50) -> list[ClientRecord]:
        return store.list_clients(limit=limit)

    @app.get("/v1/clients/{client_id}", response_model=ClientRecord)
    def get_client(client_id: str) -> ClientRecord:
        client = store.get_client(client_id)
        if client is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="client not found")
        return client

    @app.post(
        "/v1/projects",
        response_model=ProjectRecord,
        status_code=status.HTTP_201_CREATED,
        dependencies=[Depends(require_api_auth)],
    )
    def create_project(payload: ProjectCreate) -> ProjectRecord:
        try:
            return store.create_project(payload)
        except ClientNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"client not found: {exc.client_id}",
            ) from exc

    @app.get("/v1/projects", response_model=list[ProjectRecord])
    def list_projects(
        client_id: Annotated[str | None, Query(min_length=1, max_length=80)] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> list[ProjectRecord]:
        return store.list_projects(client_id=client_id, limit=limit)

    @app.get("/v1/projects/{project_id}", response_model=ProjectRecord)
    def get_project(project_id: str) -> ProjectRecord:
        project = store.get_project(project_id)
        if project is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="project not found")
        return project

    @app.get(
        "/v1/projects/{project_id}/jobs/batches",
        response_model=list[ProjectJobBatchRecord],
    )
    def list_project_job_batches(
        project_id: str,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
    ) -> list[ProjectJobBatchRecord]:
        project_or_404(project_id)
        return store.list_project_job_batches(project_id=project_id, limit=limit)

    @app.get(
        "/v1/projects/{project_id}/jobs/batches/{batch_id}",
        response_model=ProjectJobBatchRecord,
    )
    def get_project_job_batch(project_id: str, batch_id: str) -> ProjectJobBatchRecord:
        project_or_404(project_id)
        return project_batch_or_404(project_id, batch_id)

    @app.get(
        "/v1/projects/{project_id}/jobs/batches/{batch_id}/report.json",
        response_model=ProjectJobBatchReport,
        dependencies=[Depends(require_api_auth)],
    )
    def get_project_job_batch_report(
        project_id: str,
        batch_id: str,
    ) -> ProjectJobBatchReport:
        project_or_404(project_id)
        batch = project_batch_or_404(project_id, batch_id)
        return project_job_batch_report(batch=batch, spend=_xai_spend_summary(settings.media_root))

    @app.get(
        "/v1/projects/{project_id}/jobs/batches/{batch_id}/report.csv",
        dependencies=[Depends(require_api_auth)],
    )
    def get_project_job_batch_report_csv(project_id: str, batch_id: str) -> Response:
        project_or_404(project_id)
        batch = project_batch_or_404(project_id, batch_id)
        filename = f"{_safe_archive_part(batch.id)}-report.csv"
        headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
        return Response(
            project_job_batch_report_csv(
                batch=batch,
                spend=_xai_spend_summary(settings.media_root),
            ),
            media_type="text/csv; charset=utf-8",
            headers=headers,
        )

    @app.post(
        "/v1/projects/{project_id}/jobs/batch",
        response_model=ProjectJobBatchRecord,
        status_code=status.HTTP_201_CREATED,
        dependencies=[Depends(require_api_auth)],
    )
    def create_project_job_batch(
        project_id: str,
        payload: ProjectJobBatchCreate,
    ) -> ProjectJobBatchRecord:
        return create_project_job_batch_record(project_id=project_id, payload=payload, source="api")

    @app.get("/v1/catalog-import/template.csv")
    def catalog_import_template() -> Response:
        headers = {
            "Content-Disposition": "attachment; filename=\"aphrodite-catalog-template.csv\""
        }
        return Response(
            catalog_csv_template(),
            media_type="text/csv; charset=utf-8",
            headers=headers,
        )

    @app.post(
        "/v1/projects/{project_id}/jobs/batch/csv",
        response_model=ProjectJobBatchRecord,
        status_code=status.HTTP_201_CREATED,
        dependencies=[Depends(require_api_auth)],
    )
    async def create_project_job_batch_from_csv(
        project_id: str,
        file: Annotated[UploadFile, File(...)],
        marketplace_targets: Annotated[str, Form(max_length=500)] = "catalog_square",
        background_style: Annotated[str, Form(max_length=80)] = "clean_white",
        background_prompt: Annotated[str | None, Form(max_length=1000)] = None,
        quantity_per_target: Annotated[int, Form(ge=1, le=8)] = 1,
        priority: Annotated[int, Form(ge=0, le=10)] = 5,
    ) -> ProjectJobBatchRecord:
        content = await file.read(settings.max_upload_bytes + 1)
        if len(content) > settings.max_upload_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail="CSV upload is too large",
            )
        try:
            payload = parse_catalog_csv(
                content,
                marketplace_targets=split_marketplace_targets(marketplace_targets),
                background=BackgroundIntent(style=background_style, prompt=background_prompt),
                quantity_per_target=quantity_per_target,
                priority=priority,
            )
        except CatalogImportError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=exc.message,
            ) from exc
        except ValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=exc.errors()[0].get("msg", "invalid CSV defaults"),
            ) from exc
        return create_project_job_batch_record(
            project_id=project_id,
            payload=payload,
            source="api_csv",
        )

    @app.post(
        "/v1/assets",
        response_model=AssetRecord,
        status_code=status.HTTP_201_CREATED,
        dependencies=[Depends(require_api_auth)],
    )
    async def upload_asset(file: Annotated[UploadFile, File(...)]) -> AssetRecord:
        content = await file.read(settings.max_upload_bytes + 1)
        try:
            validated = validate_image_upload(
                content=content,
                filename=file.filename,
                declared_content_type=file.content_type,
                max_bytes=settings.max_upload_bytes,
            )
            asset_id = str(uuid.uuid4())
            storage_path = storage_path_for(asset_id, validated.extension)
            target_path = write_asset_file(
                media_root=settings.media_root,
                relative_path=storage_path,
                content=validated.content,
            )
            try:
                return store.create_asset(
                    asset_id=asset_id,
                    original_filename=validated.original_filename,
                    content_type=validated.content_type,
                    storage_path=storage_path,
                    bytes=validated.bytes,
                    sha256=validated.sha256,
                    width=validated.width,
                    height=validated.height,
                )
            except Exception as exc:
                target_path.unlink(missing_ok=True)
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="failed to save asset metadata",
                ) from exc
        except AssetValidationError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
        except AssetStorageError as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=str(exc),
            ) from exc

    @app.get("/v1/assets", response_model=list[AssetRecord])
    def list_assets(limit: Annotated[int, Query(ge=1, le=100)] = 50) -> list[AssetRecord]:
        return store.list_assets(limit=limit)

    @app.get("/v1/assets/{asset_id}", response_model=AssetRecord)
    def get_asset(asset_id: str) -> AssetRecord:
        asset = store.get_asset(asset_id)
        if asset is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="asset not found")
        return asset

    @app.get("/v1/marketplace-presets")
    def marketplace_presets() -> list[dict[str, str | int]]:
        return [spec.as_dict() for spec in list_marketplace_specs()]

    @app.post(
        "/v1/jobs",
        response_model=JobRecord,
        status_code=status.HTTP_201_CREATED,
        dependencies=[Depends(require_api_auth)],
    )
    def create_job(payload: JobCreate) -> JobRecord:
        try:
            return store.create_job(payload)
        except AssetNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"source asset not found: {exc.asset_id}",
            ) from exc
        except ProjectNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"project not found: {exc.project_id}",
            ) from exc

    @app.get("/v1/jobs", response_model=list[JobRecord])
    def list_jobs(
        status_filter: Annotated[JobStatus | None, Query(alias="status")] = None,
        client_id: Annotated[str | None, Query(min_length=1, max_length=80)] = None,
        project_id: Annotated[str | None, Query(min_length=1, max_length=80)] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> list[JobRecord]:
        return store.list_jobs(
            status=status_filter,
            client_id=client_id,
            project_id=project_id,
            limit=limit,
        )

    @app.get("/v1/jobs/{job_id}", response_model=JobRecord)
    def get_job(job_id: str) -> JobRecord:
        job = store.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="job not found")
        return job

    @app.patch(
        "/v1/jobs/{job_id}/status",
        response_model=JobRecord,
        dependencies=[Depends(require_api_auth)],
    )
    def update_job_status(job_id: str, payload: JobStatusUpdate) -> JobRecord:
        job = store.update_status(
            job_id,
            payload.status,
            error=payload.error,
            failure_category=payload.failure_category,
        )
        if job is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="job not found")
        process_job_batch_alerts(job)
        return job

    @app.post(
        "/v1/worker/jobs/claim",
        response_model=WorkerJobClaim | None,
        dependencies=[Depends(require_worker_auth)],
    )
    def claim_next_job(payload: WorkerClaimRequest) -> WorkerJobClaim | None:
        return store.claim_next_job(
            worker_id=payload.worker_id,
            claim_ttl_seconds=payload.claim_ttl_seconds,
        )

    @app.post(
        "/v1/worker/jobs/{job_id}/heartbeat",
        response_model=WorkerJobClaim,
        dependencies=[Depends(require_worker_auth)],
    )
    def refresh_job_claim(job_id: str, payload: WorkerClaimRefreshRequest) -> WorkerJobClaim:
        claim = store.refresh_claim(
            job_id=job_id,
            claim_token=payload.claim_token,
            claim_ttl_seconds=payload.claim_ttl_seconds,
        )
        if claim is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="job claim is not active",
            )
        return claim

    @app.post(
        "/v1/worker/jobs/{job_id}/outputs",
        response_model=JobOutputRecord,
        dependencies=[Depends(require_worker_auth)],
    )
    def complete_job_output(job_id: str, payload: JobOutputCreate) -> JobOutputRecord:
        try:
            output = store.complete_job_output(job_id=job_id, output=payload)
        except OutputVariantNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"output variant not found: {exc.variant_id}",
            ) from exc
        if output is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="job claim is not active",
            )
        return output

    @app.post(
        "/v1/worker/jobs/{job_id}/fail",
        response_model=JobRecord,
        dependencies=[Depends(require_worker_auth)],
    )
    def fail_job(job_id: str, payload: JobFailureRequest) -> JobRecord:
        job = store.fail_claimed_job(
            job_id=job_id,
            claim_token=payload.claim_token,
            error=payload.error,
            failure_category=payload.failure_category,
        )
        if job is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="job claim is not active",
            )
        process_job_batch_alerts(job)
        return job

    return app


def _xai_spend_summary(media_root: str) -> XAISpendSummary:
    return read_xai_spend_summary(
        ledger_path=xai_cost_ledger_path(media_root=media_root),
        limit=10_000,
    )


def _job_needs_review(job: JobRecord) -> bool:
    return any(output.review_status == OutputReviewStatus.PENDING_REVIEW for output in job.outputs)


def _job_output_or_404(job: JobRecord, variant_id: str) -> JobOutputRecord:
    output = next((item for item in job.outputs if item.variant_id == variant_id), None)
    if output is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="output not found")
    return output


def _require_approved_output(output: JobOutputRecord) -> None:
    if output.review_status != OutputReviewStatus.APPROVED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="output is not approved",
        )


def _media_file_response(
    *,
    media_root: str,
    relative_path: str,
    media_type: str,
    disposition: str = "inline",
) -> FileResponse:
    try:
        path = resolve_existing_media_file(media_root=media_root, relative_path=relative_path)
    except OutputStorageError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="media path is outside the media root",
        ) from exc
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="media file not found",
        ) from exc
    return FileResponse(
        path,
        media_type=media_type,
        filename=path.name,
        content_disposition_type=disposition,
    )


def _project_export_arcname(*, job: JobRecord, output: JobOutputRecord) -> str:
    product = _safe_archive_part(job.product.sku or job.product.name or job.id)
    variant = _safe_archive_part(output.variant_id)
    extension = Path(output.storage_path).suffix or _extension_for_content_type(output.content_type)
    return f"{product}-{job.id[:8]}/{variant}{extension}"


def _unique_archive_name(arcname: str, *, used: set[str]) -> str:
    if arcname not in used:
        used.add(arcname)
        return arcname
    stem = Path(arcname).with_suffix("").as_posix()
    suffix = Path(arcname).suffix
    index = 2
    while f"{stem}-{index}{suffix}" in used:
        index += 1
    unique = f"{stem}-{index}{suffix}"
    used.add(unique)
    return unique


def _safe_archive_part(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-._")
    return safe or "item"


def _extension_for_content_type(content_type: str) -> str:
    if content_type == "image/png":
        return ".png"
    if content_type == "image/jpeg":
        return ".jpg"
    return ""


def _require_bearer_token(*, authorization: str | None, expected_token: str | None) -> None:
    if not expected_token:
        return
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not secrets.compare_digest(token, expected_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
