"""HTTP API for Aphrodite."""

from __future__ import annotations

from typing import Annotated

from fastapi import FastAPI, HTTPException, Query, status

from aphrodite import __version__
from aphrodite.config import Settings
from aphrodite.domain import JobCreate, JobRecord, JobStatus, JobStatusUpdate
from aphrodite.marketplaces import list_marketplace_specs
from aphrodite.store import JobStore


def create_app(settings: Settings | None = None, store: JobStore | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
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
        return {"status": "ready", "store": "sqlite"}

    @app.get("/v1/marketplace-presets")
    def marketplace_presets() -> list[dict[str, str | int]]:
        return [spec.as_dict() for spec in list_marketplace_specs()]

    @app.post("/v1/jobs", response_model=JobRecord, status_code=status.HTTP_201_CREATED)
    def create_job(payload: JobCreate) -> JobRecord:
        return store.create_job(payload)

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

    return app
