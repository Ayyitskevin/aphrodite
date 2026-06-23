"""HTTP API for Aphrodite."""

from __future__ import annotations

import secrets
import uuid
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, File, Header, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse, HTMLResponse

from aphrodite import __version__
from aphrodite.admin import (
    XAISpendSummary,
    read_xai_spend_summary,
    render_admin_job_detail,
    render_admin_jobs_index,
    xai_cost_ledger_path,
)
from aphrodite.assets import (
    AssetStorageError,
    AssetValidationError,
    storage_path_for,
    validate_image_upload,
    write_asset_file,
)
from aphrodite.config import Settings
from aphrodite.domain import (
    AssetRecord,
    JobCreate,
    JobFailureRequest,
    JobOutputCreate,
    JobOutputRecord,
    JobRecord,
    JobStatus,
    JobStatusUpdate,
    WorkerClaimRefreshRequest,
    WorkerClaimRequest,
    WorkerJobClaim,
)
from aphrodite.marketplaces import list_marketplace_specs
from aphrodite.storage import OutputStorageError, resolve_existing_media_file
from aphrodite.store import AssetNotFoundError, JobStore, OutputVariantNotFoundError


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
    def admin_jobs(limit: Annotated[int, Query(ge=1, le=100)] = 50) -> HTMLResponse:
        spend = _xai_spend_summary(settings.media_root)
        return HTMLResponse(
            render_admin_jobs_index(jobs=store.list_jobs(limit=limit), spend=spend)
        )

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
        output = next((item for item in job.outputs if item.variant_id == variant_id), None)
        if output is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="output not found")
        return _media_file_response(
            media_root=settings.media_root,
            relative_path=output.storage_path,
            media_type=output.content_type,
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

    @app.get("/v1/jobs", response_model=list[JobRecord])
    def list_jobs(
        status_filter: Annotated[JobStatus | None, Query(alias="status")] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> list[JobRecord]:
        return store.list_jobs(status=status_filter, limit=limit)

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
        job = store.update_status(job_id, payload.status, error=payload.error)
        if job is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="job not found")
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
        )
        if job is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="job claim is not active",
            )
        return job

    return app


def _xai_spend_summary(media_root: str) -> XAISpendSummary:
    return read_xai_spend_summary(ledger_path=xai_cost_ledger_path(media_root=media_root))


def _media_file_response(*, media_root: str, relative_path: str, media_type: str) -> FileResponse:
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
        content_disposition_type="inline",
    )


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
