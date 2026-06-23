"""HTTP API for Aphrodite."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, File, HTTPException, Query, UploadFile, status

from aphrodite import __version__
from aphrodite.assets import (
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

    @app.post("/v1/assets", response_model=AssetRecord, status_code=status.HTTP_201_CREATED)
    async def upload_asset(file: Annotated[UploadFile, File(...)]) -> AssetRecord:
        content = await file.read(settings.max_upload_bytes + 1)
        try:
            validated = validate_image_upload(
                content=content,
                filename=file.filename,
                declared_content_type=file.content_type,
                max_bytes=settings.max_upload_bytes,
            )
        except AssetValidationError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc

        asset = store.create_asset(
            original_filename=validated.original_filename,
            content_type=validated.content_type,
            storage_path="pending",
            bytes=validated.bytes,
            sha256=validated.sha256,
            width=validated.width,
            height=validated.height,
        )
        storage_path = storage_path_for(asset.id, validated.extension)
        write_asset_file(
            media_root=settings.media_root,
            relative_path=storage_path,
            content=validated.content,
        )
        return store.update_asset_storage_path(asset.id, storage_path)

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

    @app.post("/v1/jobs", response_model=JobRecord, status_code=status.HTTP_201_CREATED)
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

    @app.patch("/v1/jobs/{job_id}/status", response_model=JobRecord)
    def update_job_status(job_id: str, payload: JobStatusUpdate) -> JobRecord:
        job = store.update_status(job_id, payload.status, error=payload.error)
        if job is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="job not found")
        return job

    @app.post("/v1/worker/jobs/claim", response_model=WorkerJobClaim | None)
    def claim_next_job(payload: WorkerClaimRequest) -> WorkerJobClaim | None:
        return store.claim_next_job(
            worker_id=payload.worker_id,
            claim_ttl_seconds=payload.claim_ttl_seconds,
        )

    @app.post("/v1/worker/jobs/{job_id}/heartbeat", response_model=WorkerJobClaim)
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

    @app.post("/v1/worker/jobs/{job_id}/outputs", response_model=JobOutputRecord)
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

    @app.post("/v1/worker/jobs/{job_id}/fail", response_model=JobRecord)
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
