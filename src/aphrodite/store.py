"""SQLite persistence for Aphrodite assets and jobs."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import BaseModel

from aphrodite.domain import (
    AssetRecord,
    ClientCreate,
    ClientRecord,
    JobCreate,
    JobFailureCategory,
    JobOutputCreate,
    JobOutputRecord,
    JobRecord,
    JobStatus,
    OutputReviewStatus,
    OutputStatus,
    OutputVariant,
    ProductInput,
    ProjectCreate,
    ProjectJobBatchAlert,
    ProjectJobBatchAlertRecord,
    ProjectJobBatchCreate,
    ProjectJobBatchRecord,
    ProjectRecord,
    WorkerJobClaim,
    build_output_plan,
)
from aphrodite.failures import classify_failure

SCHEMA = """
CREATE TABLE IF NOT EXISTS assets (
  id TEXT PRIMARY KEY,
  original_filename TEXT NOT NULL,
  content_type TEXT NOT NULL,
  storage_path TEXT NOT NULL,
  bytes INTEGER NOT NULL,
  sha256 TEXT NOT NULL,
  width INTEGER NOT NULL,
  height INTEGER NOT NULL,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_assets_created_at
  ON assets(created_at);

CREATE TABLE IF NOT EXISTS clients (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  external_id TEXT,
  notes TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_clients_name
  ON clients(name);

CREATE TABLE IF NOT EXISTS projects (
  id TEXT PRIMARY KEY,
  client_id TEXT NOT NULL,
  name TEXT NOT NULL,
  external_id TEXT,
  notes TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(client_id) REFERENCES clients(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_projects_client
  ON projects(client_id);

CREATE INDEX IF NOT EXISTS idx_projects_name
  ON projects(name);

CREATE TABLE IF NOT EXISTS project_job_batches (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  source TEXT NOT NULL,
  defaults_json TEXT NOT NULL,
  item_count INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_project_job_batches_project
  ON project_job_batches(project_id, created_at);

CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  status TEXT NOT NULL,
  product_name TEXT NOT NULL,
  product_sku TEXT,
  source_image_uri TEXT,
  source_asset_id TEXT,
  project_id TEXT,
  batch_id TEXT,
  payload_json TEXT NOT NULL,
  output_plan_json TEXT NOT NULL,
  priority INTEGER NOT NULL,
  idempotency_key TEXT,
  claimed_by TEXT,
  claim_token TEXT,
  claimed_at TEXT,
  claim_expires_at TEXT,
  error TEXT,
  failure_category TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(source_asset_id) REFERENCES assets(id),
  FOREIGN KEY(project_id) REFERENCES projects(id),
  FOREIGN KEY(batch_id) REFERENCES project_job_batches(id)
);

CREATE INDEX IF NOT EXISTS idx_jobs_status_updated
  ON jobs(status, updated_at);


CREATE TABLE IF NOT EXISTS job_outputs (
  id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL,
  variant_id TEXT NOT NULL,
  status TEXT NOT NULL,
  storage_path TEXT NOT NULL,
  content_type TEXT NOT NULL,
  bytes INTEGER NOT NULL,
  sha256 TEXT NOT NULL,
  width INTEGER NOT NULL,
  height INTEGER NOT NULL,
  cost_usd REAL NOT NULL DEFAULT 0,
  cost_ticks INTEGER,
  model TEXT,
  latency_ms INTEGER,
  render_request_id TEXT,
  error TEXT,
  review_status TEXT NOT NULL DEFAULT 'pending_review',
  review_note TEXT,
  reviewed_at TEXT,
  rights_confirmed_at TEXT,
  rights_confirmed_by TEXT,
  license_note TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(job_id, variant_id),
  FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_job_outputs_job
  ON job_outputs(job_id);

CREATE TABLE IF NOT EXISTS project_job_batch_alerts (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  batch_id TEXT NOT NULL,
  level TEXT NOT NULL,
  code TEXT NOT NULL,
  message TEXT NOT NULL,
  count INTEGER NOT NULL,
  last_seen_at TEXT NOT NULL,
  delivered_at TEXT,
  delivery_attempted_at TEXT,
  delivery_error TEXT,
  delivery_attempt_count INTEGER NOT NULL DEFAULT 0,
  next_delivery_attempt_at TEXT,
  acknowledged_at TEXT,
  acknowledged_by TEXT,
  muted_until TEXT,
  resolved_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(batch_id, code),
  FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
  FOREIGN KEY(batch_id) REFERENCES project_job_batches(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_project_job_batch_alerts_batch
  ON project_job_batch_alerts(project_id, batch_id, level);
"""


class AssetNotFoundError(Exception):
    def __init__(self, asset_id: str) -> None:
        super().__init__(f"asset not found: {asset_id}")
        self.asset_id = asset_id


class ClientNotFoundError(Exception):
    def __init__(self, client_id: str) -> None:
        super().__init__(f"client not found: {client_id}")
        self.client_id = client_id


class ProjectNotFoundError(Exception):
    def __init__(self, project_id: str) -> None:
        super().__init__(f"project not found: {project_id}")
        self.project_id = project_id


class OutputVariantNotFoundError(Exception):
    def __init__(self, job_id: str, variant_id: str) -> None:
        super().__init__(f"output variant not found for job {job_id}: {variant_id}")
        self.job_id = job_id
        self.variant_id = variant_id


def _utc_now() -> str:
    return _utc_at(datetime.now(UTC))


def _utc_from_now(seconds: int) -> str:
    return _utc_at(datetime.now(UTC) + timedelta(seconds=seconds))


def _utc_at(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _jsonable(model: BaseModel) -> dict:
    return model.model_dump(mode="json")


def _failure_category_value(
    *,
    error: str | None,
    failure_category: JobFailureCategory | str | None,
) -> str:
    if failure_category is not None:
        try:
            return JobFailureCategory(failure_category).value
        except ValueError:
            pass
    return classify_failure(error).value


class JobStore:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

    def initialize(self) -> None:
        self._ensure_parent()
        with self._connect() as conn:
            if self.db_path != ":memory:":
                conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
            self._migrate(conn)

    def ping(self) -> bool:
        try:
            with self._connect() as conn:
                conn.execute("SELECT 1").fetchone()
        except sqlite3.Error:
            return False
        return True

    def create_asset(
        self,
        *,
        original_filename: str,
        content_type: str,
        storage_path: str,
        bytes: int,
        sha256: str,
        width: int,
        height: int,
        asset_id: str | None = None,
    ) -> AssetRecord:
        asset = AssetRecord(
            id=asset_id or str(uuid.uuid4()),
            original_filename=original_filename,
            content_type=content_type,
            storage_path=storage_path,
            bytes=bytes,
            sha256=sha256,
            width=width,
            height=height,
            created_at=_utc_now(),
        )
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO assets (
                  id, original_filename, content_type, storage_path,
                  bytes, sha256, width, height, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    asset.id,
                    asset.original_filename,
                    asset.content_type,
                    asset.storage_path,
                    asset.bytes,
                    asset.sha256,
                    asset.width,
                    asset.height,
                    asset.created_at,
                ),
            )
        return asset

    def get_asset(self, asset_id: str) -> AssetRecord | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
        return self._row_to_asset(row) if row is not None else None

    def list_assets(self, *, limit: int = 50) -> list[AssetRecord]:
        bounded_limit = max(1, min(limit, 100))
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM assets ORDER BY created_at DESC LIMIT ?",
                (bounded_limit,),
            ).fetchall()
        return [self._row_to_asset(row) for row in rows]

    def update_asset_storage_path(self, asset_id: str, storage_path: str) -> AssetRecord:
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE assets SET storage_path = ? WHERE id = ?",
                (storage_path, asset_id),
            )
            if cursor.rowcount == 0:
                raise AssetNotFoundError(asset_id)
        asset = self.get_asset(asset_id)
        if asset is None:
            raise AssetNotFoundError(asset_id)
        return asset

    def create_client(
        self,
        request: ClientCreate,
        *,
        client_id: str | None = None,
    ) -> ClientRecord:
        now = _utc_now()
        client = ClientRecord(
            id=client_id or str(uuid.uuid4()),
            name=request.name,
            external_id=request.external_id,
            notes=request.notes,
            created_at=now,
            updated_at=now,
        )
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO clients (
                  id, name, external_id, notes, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    client.id,
                    client.name,
                    client.external_id,
                    client.notes,
                    client.created_at,
                    client.updated_at,
                ),
            )
        return client

    def get_client(self, client_id: str) -> ClientRecord | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()
        return self._row_to_client(row) if row is not None else None

    def list_clients(self, *, limit: int = 50) -> list[ClientRecord]:
        bounded_limit = max(1, min(limit, 100))
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM clients ORDER BY created_at DESC LIMIT ?",
                (bounded_limit,),
            ).fetchall()
        return [self._row_to_client(row) for row in rows]

    def create_project(
        self,
        request: ProjectCreate,
        *,
        project_id: str | None = None,
    ) -> ProjectRecord:
        client = self.get_client(request.client_id)
        if client is None:
            raise ClientNotFoundError(request.client_id)

        now = _utc_now()
        project = ProjectRecord(
            id=project_id or str(uuid.uuid4()),
            client_id=request.client_id,
            client=client,
            name=request.name,
            external_id=request.external_id,
            notes=request.notes,
            created_at=now,
            updated_at=now,
        )
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO projects (
                  id, client_id, name, external_id, notes, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    project.id,
                    project.client_id,
                    project.name,
                    project.external_id,
                    project.notes,
                    project.created_at,
                    project.updated_at,
                ),
            )
        return project

    def get_project(self, project_id: str) -> ProjectRecord | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        return self._row_to_project(row) if row is not None else None

    def list_projects(
        self,
        *,
        client_id: str | None = None,
        limit: int = 50,
    ) -> list[ProjectRecord]:
        bounded_limit = max(1, min(limit, 100))
        with self._connect() as conn:
            if client_id is None:
                rows = conn.execute(
                    "SELECT * FROM projects ORDER BY created_at DESC LIMIT ?",
                    (bounded_limit,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT *
                      FROM projects
                     WHERE client_id = ?
                     ORDER BY created_at DESC
                     LIMIT ?
                    """,
                    (client_id, bounded_limit),
                ).fetchall()
        return [self._row_to_project(row) for row in rows]

    def create_job(self, request: JobCreate) -> JobRecord:
        # Idempotent create: re-submitting the same request key returns the
        # existing job instead of spawning a second one, so a caller (Mise)
        # retry cannot duplicate renders or double-charge.
        if request.idempotency_key is not None:
            existing = self.get_job_by_idempotency_key(request.idempotency_key)
            if existing is not None:
                return existing

        source_asset = None
        if request.source_asset_id is not None:
            source_asset = self.get_asset(request.source_asset_id)
            if source_asset is None:
                raise AssetNotFoundError(request.source_asset_id)

        project = None
        if request.project_id is not None:
            project = self.get_project(request.project_id)
            if project is None:
                raise ProjectNotFoundError(request.project_id)

        output_plan = build_output_plan(request)
        now = _utc_now()
        job = JobRecord(
            id=str(uuid.uuid4()),
            status=JobStatus.QUEUED,
            product=request.product,
            source_asset_id=request.source_asset_id,
            source_asset=source_asset,
            project_id=request.project_id,
            project=project,
            batch_id=None,
            marketplace_targets=request.marketplace_targets,
            output_plan=output_plan,
            priority=request.priority,
            idempotency_key=request.idempotency_key,
            created_at=now,
            updated_at=now,
        )
        source_image_uri = request.product.source_image_uri or f"asset://{request.source_asset_id}"

        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO jobs (
                      id, status, product_name, product_sku, source_image_uri, source_asset_id,
                      project_id, batch_id, payload_json, output_plan_json, priority,
                      idempotency_key, error, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        job.id,
                        job.status.value,
                        request.product.name,
                        request.product.sku,
                        source_image_uri,
                        request.source_asset_id,
                        request.project_id,
                        None,
                        json.dumps(_jsonable(request), sort_keys=True),
                        json.dumps([_jsonable(variant) for variant in output_plan], sort_keys=True),
                        request.priority,
                        request.idempotency_key,
                        None,
                        now,
                        now,
                    ),
                )
        except sqlite3.IntegrityError:
            # A concurrent create with the same idempotency key won the race;
            # return that job rather than failing the retry.
            if request.idempotency_key is not None:
                existing = self.get_job_by_idempotency_key(request.idempotency_key)
                if existing is not None:
                    return existing
            raise

        return job

    def get_job_by_idempotency_key(self, idempotency_key: str) -> JobRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
        return self._row_to_job(row) if row is not None else None

    def create_project_job_batch(
        self,
        *,
        project_id: str,
        request: ProjectJobBatchCreate,
        source: str = "api",
        batch_id: str | None = None,
    ) -> list[JobRecord]:
        project = self.get_project(project_id)
        if project is None:
            raise ProjectNotFoundError(project_id)

        batch_id = batch_id or str(uuid.uuid4())
        clean_source = source.strip() if source.strip() else "api"
        batch_defaults = {
            "marketplace_targets": request.marketplace_targets,
            "background": _jsonable(request.background),
            "quantity_per_target": request.quantity_per_target,
            "priority": request.priority,
        }

        prepared: list[tuple[JobCreate, AssetRecord | None, list[OutputVariant]]] = []
        for item in request.items:
            job_request = JobCreate(
                product=item.product,
                source_asset_id=item.source_asset_id,
                project_id=project_id,
                marketplace_targets=item.marketplace_targets or request.marketplace_targets,
                background=item.background or request.background,
                quantity_per_target=item.quantity_per_target or request.quantity_per_target,
                priority=item.priority if item.priority is not None else request.priority,
            )
            source_asset = None
            if job_request.source_asset_id is not None:
                source_asset = self.get_asset(job_request.source_asset_id)
                if source_asset is None:
                    raise AssetNotFoundError(job_request.source_asset_id)
            prepared.append((job_request, source_asset, build_output_plan(job_request)))

        now = _utc_now()
        jobs: list[JobRecord] = []
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT INTO project_job_batches (
                  id, project_id, source, defaults_json, item_count, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    batch_id,
                    project_id,
                    clean_source,
                    json.dumps(batch_defaults, sort_keys=True),
                    len(prepared),
                    now,
                    now,
                ),
            )
            for job_request, source_asset, output_plan in prepared:
                job = JobRecord(
                    id=str(uuid.uuid4()),
                    status=JobStatus.QUEUED,
                    product=job_request.product,
                    source_asset_id=job_request.source_asset_id,
                    source_asset=source_asset,
                    project_id=project_id,
                    project=project,
                    batch_id=batch_id,
                    marketplace_targets=job_request.marketplace_targets,
                    output_plan=output_plan,
                    priority=job_request.priority,
                    created_at=now,
                    updated_at=now,
                )
                source_image_uri = (
                    job_request.product.source_image_uri
                    or f"asset://{job_request.source_asset_id}"
                )
                conn.execute(
                    """
                    INSERT INTO jobs (
                      id, status, product_name, product_sku, source_image_uri, source_asset_id,
                      project_id, batch_id, payload_json, output_plan_json, priority, error,
                      created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        job.id,
                        job.status.value,
                        job_request.product.name,
                        job_request.product.sku,
                        source_image_uri,
                        job_request.source_asset_id,
                        project_id,
                        batch_id,
                        json.dumps(_jsonable(job_request), sort_keys=True),
                        json.dumps(
                            [_jsonable(variant) for variant in output_plan],
                            sort_keys=True,
                        ),
                        job_request.priority,
                        None,
                        now,
                        now,
                    ),
                )
                jobs.append(job)

        return jobs

    def get_job(self, job_id: str) -> JobRecord | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return self._row_to_job(row) if row is not None else None

    def list_jobs(
        self,
        *,
        status: JobStatus | None = None,
        project_id: str | None = None,
        client_id: str | None = None,
        batch_id: str | None = None,
        limit: int = 50,
    ) -> list[JobRecord]:
        bounded_limit = max(1, min(limit, 100))
        query = "SELECT jobs.* FROM jobs"
        params: list[str | int] = []
        where: list[str] = []

        if client_id is not None:
            query += " JOIN projects ON projects.id = jobs.project_id"
            where.append("projects.client_id = ?")
            params.append(client_id)
        if project_id is not None:
            where.append("jobs.project_id = ?")
            params.append(project_id)
        if batch_id is not None:
            where.append("jobs.batch_id = ?")
            params.append(batch_id)
        if status is not None:
            where.append("jobs.status = ?")
            params.append(status.value)

        if where:
            query += " WHERE " + " AND ".join(where)
        query += " ORDER BY jobs.created_at DESC LIMIT ?"
        params.append(bounded_limit)

        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()

        return [self._row_to_job(row) for row in rows]

    def get_project_job_batch(self, batch_id: str) -> ProjectJobBatchRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM project_job_batches WHERE id = ?",
                (batch_id,),
            ).fetchone()
        return self._row_to_project_job_batch(row) if row is not None else None

    def list_project_job_batches(
        self,
        *,
        project_id: str,
        limit: int = 20,
    ) -> list[ProjectJobBatchRecord]:
        bounded_limit = max(1, min(limit, 100))
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                  FROM project_job_batches
                 WHERE project_id = ?
                 ORDER BY created_at DESC
                 LIMIT ?
                """,
                (project_id, bounded_limit),
            ).fetchall()
        return [self._row_to_project_job_batch(row) for row in rows]

    def list_job_outputs(self, job_id: str) -> list[JobOutputRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM job_outputs WHERE job_id = ? ORDER BY created_at ASC",
                (job_id,),
            ).fetchall()
        return [self._row_to_output(row) for row in rows]

    def claim_next_job(
        self,
        *,
        worker_id: str,
        claim_ttl_seconds: int = 300,
    ) -> WorkerJobClaim | None:
        now = _utc_now()
        expires_at = _utc_from_now(claim_ttl_seconds)
        claim_token = str(uuid.uuid4())

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT *
                  FROM jobs
                 WHERE status = ?
                    OR (status = ? AND claim_expires_at IS NOT NULL AND claim_expires_at <= ?)
                 ORDER BY priority DESC, created_at ASC
                 LIMIT 1
                """,
                (JobStatus.QUEUED.value, JobStatus.RENDERING.value, now),
            ).fetchone()
            if row is None:
                return None

            conn.execute(
                """
                UPDATE jobs
                   SET status = ?,
                       claimed_by = ?,
                       claim_token = ?,
                       claimed_at = ?,
                       claim_expires_at = ?,
                       error = NULL,
                       failure_category = NULL,
                       updated_at = ?
                 WHERE id = ?
                """,
                (
                    JobStatus.RENDERING.value,
                    worker_id,
                    claim_token,
                    now,
                    expires_at,
                    now,
                    row["id"],
                ),
            )
            job_id = row["id"]

        job = self.get_job(job_id)
        if job is None:
            return None
        return WorkerJobClaim(job=job, claim_token=claim_token, claim_expires_at=expires_at)

    def refresh_claim(
        self,
        *,
        job_id: str,
        claim_token: str,
        claim_ttl_seconds: int = 300,
    ) -> WorkerJobClaim | None:
        now = _utc_now()
        expires_at = _utc_from_now(claim_ttl_seconds)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                """
                UPDATE jobs
                   SET claim_expires_at = ?, updated_at = ?
                 WHERE id = ?
                   AND status = ?
                   AND claim_token = ?
                   AND claim_expires_at > ?
                """,
                (
                    expires_at,
                    now,
                    job_id,
                    JobStatus.RENDERING.value,
                    claim_token,
                    now,
                ),
            )
            if cursor.rowcount == 0:
                return None

        job = self.get_job(job_id)
        if job is None:
            return None
        return WorkerJobClaim(job=job, claim_token=claim_token, claim_expires_at=expires_at)

    def complete_job_output(
        self,
        *,
        job_id: str,
        output: JobOutputCreate,
    ) -> JobOutputRecord | None:
        now = _utc_now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            job_row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if job_row is None or not self._has_active_claim(job_row, output.claim_token, now):
                return None

            planned_variant_ids = {
                variant["id"]
                for variant in json.loads(job_row["output_plan_json"])
            }
            if output.variant_id not in planned_variant_ids:
                raise OutputVariantNotFoundError(job_id, output.variant_id)

            existing = conn.execute(
                "SELECT id, created_at, render_request_id "
                "FROM job_outputs WHERE job_id = ? AND variant_id = ?",
                (job_id, output.variant_id),
            ).fetchone()
            is_duplicate_delivery = (
                existing is not None
                and output.render_request_id is not None
                and existing["render_request_id"] == output.render_request_id
            )
            if not is_duplicate_delivery:
                # A new render attempt (or a legacy worker that sends no
                # render_request_id) replaces the output and resets review. An
                # exact re-delivery of the same attempt falls through untouched
                # so a duplicated completion never revokes an operator's approval
                # or double-counts spend.
                output_id = existing["id"] if existing is not None else str(uuid.uuid4())
                created_at = existing["created_at"] if existing is not None else now
                conn.execute(
                    """
                    INSERT INTO job_outputs (
                      id, job_id, variant_id, status, storage_path, content_type,
                      bytes, sha256, width, height, cost_usd, cost_ticks, model,
                      latency_ms, render_request_id, error, review_status,
                      review_note, reviewed_at, rights_confirmed_at,
                      rights_confirmed_by, license_note, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(job_id, variant_id) DO UPDATE SET
                      status = excluded.status,
                      storage_path = excluded.storage_path,
                      content_type = excluded.content_type,
                      bytes = excluded.bytes,
                      sha256 = excluded.sha256,
                      width = excluded.width,
                      height = excluded.height,
                      cost_usd = excluded.cost_usd,
                      cost_ticks = excluded.cost_ticks,
                      model = excluded.model,
                      latency_ms = excluded.latency_ms,
                      render_request_id = excluded.render_request_id,
                      error = excluded.error,
                      review_status = excluded.review_status,
                      review_note = excluded.review_note,
                      reviewed_at = excluded.reviewed_at,
                      rights_confirmed_at = excluded.rights_confirmed_at,
                      rights_confirmed_by = excluded.rights_confirmed_by,
                      license_note = excluded.license_note,
                      updated_at = excluded.updated_at
                    """,
                    (
                        output_id,
                        job_id,
                        output.variant_id,
                        OutputStatus.COMPLETED.value,
                        output.storage_path,
                        output.content_type,
                        output.bytes,
                        output.sha256,
                        output.width,
                        output.height,
                        output.cost_usd,
                        output.cost_ticks,
                        output.model,
                        output.latency_ms,
                        output.render_request_id,
                        None,
                        OutputReviewStatus.PENDING_REVIEW.value,
                        None,
                        None,
                        None,
                        None,
                        None,
                        created_at,
                        now,
                    ),
                )

                completed_variant_ids = {
                    row["variant_id"]
                    for row in conn.execute(
                        """
                        SELECT variant_id
                          FROM job_outputs
                         WHERE job_id = ? AND status = ?
                        """,
                        (job_id, OutputStatus.COMPLETED.value),
                    ).fetchall()
                }
                if planned_variant_ids.issubset(completed_variant_ids):
                    conn.execute(
                        """
                        UPDATE jobs
                           SET status = ?,
                               claimed_by = NULL,
                               claim_token = NULL,
                               claimed_at = NULL,
                               claim_expires_at = NULL,
                               error = NULL,
                               failure_category = NULL,
                               updated_at = ?
                         WHERE id = ?
                        """,
                        (JobStatus.COMPLETED.value, now, job_id),
                    )

        output_record = self._get_output(job_id=job_id, variant_id=output.variant_id)
        if output_record is None:
            return None
        return output_record

    def review_output(
        self,
        *,
        job_id: str,
        variant_id: str,
        review_status: OutputReviewStatus,
        note: str | None = None,
    ) -> JobOutputRecord | None:
        now = _utc_now()
        clean_note = note.strip() if note is not None and note.strip() else None
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE job_outputs
                   SET review_status = ?,
                       review_note = ?,
                       reviewed_at = ?,
                       updated_at = ?
                 WHERE job_id = ?
                   AND variant_id = ?
                   AND status = ?
                """,
                (
                    review_status.value,
                    clean_note,
                    now,
                    now,
                    job_id,
                    variant_id,
                    OutputStatus.COMPLETED.value,
                ),
            )
            if cursor.rowcount == 0:
                return None
        return self._get_output(job_id=job_id, variant_id=variant_id)

    def confirm_output_rights(
        self,
        *,
        job_id: str,
        variant_id: str,
        confirmed_by: str,
        license_note: str | None = None,
    ) -> JobOutputRecord | None:
        now = _utc_now()
        clean_note = (
            license_note.strip() if license_note is not None and license_note.strip() else None
        )
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE job_outputs
                   SET rights_confirmed_at = ?,
                       rights_confirmed_by = ?,
                       license_note = ?,
                       updated_at = ?
                 WHERE job_id = ?
                   AND variant_id = ?
                   AND status = ?
                """,
                (
                    now,
                    confirmed_by,
                    clean_note,
                    now,
                    job_id,
                    variant_id,
                    OutputStatus.COMPLETED.value,
                ),
            )
            if cursor.rowcount == 0:
                return None
        return self._get_output(job_id=job_id, variant_id=variant_id)

    def fail_claimed_job(
        self,
        *,
        job_id: str,
        claim_token: str,
        error: str,
        failure_category: JobFailureCategory | str | None = None,
    ) -> JobRecord | None:
        now = _utc_now()
        category = _failure_category_value(error=error, failure_category=failure_category)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            job_row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if job_row is None or not self._has_active_claim(job_row, claim_token, now):
                return None
            conn.execute(
                """
                UPDATE jobs
                   SET status = ?,
                       claimed_by = NULL,
                       claim_token = NULL,
                       claimed_at = NULL,
                       claim_expires_at = NULL,
                       error = ?,
                       failure_category = ?,
                       updated_at = ?
                 WHERE id = ?
                """,
                (JobStatus.FAILED.value, error, category, now, job_id),
            )
        return self.get_job(job_id)

    def update_status(
        self,
        job_id: str,
        status: JobStatus,
        *,
        error: str | None = None,
        failure_category: JobFailureCategory | str | None = None,
    ) -> JobRecord | None:
        now = _utc_now()
        clear_claim = status != JobStatus.RENDERING
        category = (
            _failure_category_value(error=error, failure_category=failure_category)
            if status == JobStatus.FAILED
            else None
        )
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE jobs
                   SET status = ?,
                       error = ?,
                       failure_category = ?,
                       updated_at = ?,
                       claimed_by = CASE WHEN ? THEN NULL ELSE claimed_by END,
                       claim_token = CASE WHEN ? THEN NULL ELSE claim_token END,
                       claimed_at = CASE WHEN ? THEN NULL ELSE claimed_at END,
                       claim_expires_at = CASE WHEN ? THEN NULL ELSE claim_expires_at END
                 WHERE id = ?
                """,
                (
                    status.value,
                    error,
                    category,
                    now,
                    clear_claim,
                    clear_claim,
                    clear_claim,
                    clear_claim,
                    job_id,
                ),
            )
            if cursor.rowcount == 0:
                return None

        return self.get_job(job_id)

    def upsert_project_job_batch_alerts(
        self,
        *,
        project_id: str,
        batch_id: str,
        alerts: list[ProjectJobBatchAlert],
    ) -> list[ProjectJobBatchAlertRecord]:
        now = _utc_now()
        active_codes = [alert.code for alert in alerts]
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for alert in alerts:
                alert_id = str(uuid.uuid4())
                conn.execute(
                    """
                    INSERT INTO project_job_batch_alerts (
                      id, project_id, batch_id, level, code, message, count,
                      last_seen_at, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(batch_id, code) DO UPDATE SET
                      level = excluded.level,
                      message = excluded.message,
                      count = excluded.count,
                      last_seen_at = excluded.last_seen_at,
                      delivered_at = CASE
                        WHEN project_job_batch_alerts.resolved_at IS NOT NULL THEN NULL
                        ELSE project_job_batch_alerts.delivered_at
                      END,
                      delivery_attempted_at = CASE
                        WHEN project_job_batch_alerts.resolved_at IS NOT NULL THEN NULL
                        ELSE project_job_batch_alerts.delivery_attempted_at
                      END,
                      delivery_error = CASE
                        WHEN project_job_batch_alerts.resolved_at IS NOT NULL THEN NULL
                        ELSE project_job_batch_alerts.delivery_error
                      END,
                      delivery_attempt_count = CASE
                        WHEN project_job_batch_alerts.resolved_at IS NOT NULL THEN 0
                        ELSE project_job_batch_alerts.delivery_attempt_count
                      END,
                      next_delivery_attempt_at = CASE
                        WHEN project_job_batch_alerts.resolved_at IS NOT NULL THEN NULL
                        ELSE project_job_batch_alerts.next_delivery_attempt_at
                      END,
                      acknowledged_at = CASE
                        WHEN project_job_batch_alerts.resolved_at IS NOT NULL THEN NULL
                        ELSE project_job_batch_alerts.acknowledged_at
                      END,
                      acknowledged_by = CASE
                        WHEN project_job_batch_alerts.resolved_at IS NOT NULL THEN NULL
                        ELSE project_job_batch_alerts.acknowledged_by
                      END,
                      muted_until = CASE
                        WHEN project_job_batch_alerts.resolved_at IS NOT NULL THEN NULL
                        ELSE project_job_batch_alerts.muted_until
                      END,
                      resolved_at = NULL,
                      updated_at = excluded.updated_at
                    """,
                    (
                        alert_id,
                        project_id,
                        batch_id,
                        alert.level,
                        alert.code,
                        alert.message,
                        alert.count,
                        now,
                        now,
                        now,
                    ),
                )
            if active_codes:
                placeholders = ", ".join("?" for _ in active_codes)
                conn.execute(
                    f"""
                    UPDATE project_job_batch_alerts
                       SET resolved_at = ?,
                           updated_at = ?
                     WHERE project_id = ?
                       AND batch_id = ?
                       AND resolved_at IS NULL
                       AND code NOT IN ({placeholders})
                    """,
                    (now, now, project_id, batch_id, *active_codes),
                )
            else:
                conn.execute(
                    """
                    UPDATE project_job_batch_alerts
                       SET resolved_at = ?,
                           updated_at = ?
                     WHERE project_id = ?
                       AND batch_id = ?
                       AND resolved_at IS NULL
                    """,
                    (now, now, project_id, batch_id),
                )
        return self.list_project_job_batch_alerts(project_id=project_id, batch_id=batch_id)

    def list_project_job_batch_alerts(
        self,
        *,
        project_id: str,
        batch_id: str,
        include_resolved: bool = False,
        resolved_only: bool = False,
    ) -> list[ProjectJobBatchAlertRecord]:
        if resolved_only:
            resolved_filter = "AND resolved_at IS NOT NULL"
        elif include_resolved:
            resolved_filter = ""
        else:
            resolved_filter = "AND resolved_at IS NULL"
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT *
                  FROM project_job_batch_alerts
                 WHERE project_id = ? AND batch_id = ? {resolved_filter}
                 ORDER BY resolved_at IS NOT NULL,
                          CASE level WHEN 'critical' THEN 0 ELSE 1 END,
                          created_at ASC
                """,
                (project_id, batch_id),
            ).fetchall()
        return [self._row_to_project_job_batch_alert(row) for row in rows]

    def list_active_project_job_batch_alerts(
        self,
        *,
        limit: int = 100,
    ) -> list[ProjectJobBatchAlertRecord]:
        bounded_limit = max(1, min(limit, 500))
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                  FROM project_job_batch_alerts
                 WHERE resolved_at IS NULL
                 ORDER BY CASE level WHEN 'critical' THEN 0 ELSE 1 END,
                          last_seen_at DESC
                 LIMIT ?
                """,
                (bounded_limit,),
            ).fetchall()
        return [self._row_to_project_job_batch_alert(row) for row in rows]

    def acknowledge_project_job_batch_alert(
        self,
        *,
        project_id: str,
        batch_id: str,
        alert_id: str,
        acknowledged_by: str = "operator",
    ) -> ProjectJobBatchAlertRecord | None:
        now = _utc_now()
        actor = acknowledged_by.strip() if acknowledged_by.strip() else "operator"
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE project_job_batch_alerts
                   SET acknowledged_at = ?,
                       acknowledged_by = ?,
                       updated_at = ?
                 WHERE id = ? AND project_id = ? AND batch_id = ?
                """,
                (now, actor, now, alert_id, project_id, batch_id),
            )
            if cursor.rowcount == 0:
                return None
        return self.get_project_job_batch_alert(
            project_id=project_id,
            batch_id=batch_id,
            alert_id=alert_id,
        )

    def mute_project_job_batch_alert(
        self,
        *,
        project_id: str,
        batch_id: str,
        alert_id: str,
        hours: int = 24,
    ) -> ProjectJobBatchAlertRecord | None:
        now = _utc_now()
        muted_until = _utc_from_now(max(1, min(hours, 720)) * 3600)
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE project_job_batch_alerts
                   SET muted_until = ?,
                       updated_at = ?
                 WHERE id = ? AND project_id = ? AND batch_id = ?
                """,
                (muted_until, now, alert_id, project_id, batch_id),
            )
            if cursor.rowcount == 0:
                return None
        return self.get_project_job_batch_alert(
            project_id=project_id,
            batch_id=batch_id,
            alert_id=alert_id,
        )

    def clear_project_job_batch_alert_mute(
        self,
        *,
        project_id: str,
        batch_id: str,
        alert_id: str,
    ) -> ProjectJobBatchAlertRecord | None:
        now = _utc_now()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE project_job_batch_alerts
                   SET muted_until = NULL,
                       updated_at = ?
                 WHERE id = ? AND project_id = ? AND batch_id = ?
                """,
                (now, alert_id, project_id, batch_id),
            )
            if cursor.rowcount == 0:
                return None
        return self.get_project_job_batch_alert(
            project_id=project_id,
            batch_id=batch_id,
            alert_id=alert_id,
        )

    def get_project_job_batch_alert(
        self,
        *,
        project_id: str,
        batch_id: str,
        alert_id: str,
    ) -> ProjectJobBatchAlertRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT *
                  FROM project_job_batch_alerts
                 WHERE id = ? AND project_id = ? AND batch_id = ?
                """,
                (alert_id, project_id, batch_id),
            ).fetchone()
        return self._row_to_project_job_batch_alert(row) if row is not None else None

    def mark_project_job_batch_alert_delivery(
        self,
        *,
        alert_id: str,
        delivered: bool,
        error: str | None,
        next_delivery_attempt_at: str | None = None,
    ) -> ProjectJobBatchAlertRecord | None:
        now = _utc_now()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE project_job_batch_alerts
                   SET delivery_attempted_at = ?,
                       delivered_at = ?,
                       delivery_error = ?,
                       delivery_attempt_count = delivery_attempt_count + 1,
                       next_delivery_attempt_at = ?,
                       updated_at = ?
                 WHERE id = ?
                """,
                (
                    now,
                    now if delivered else None,
                    error,
                    next_delivery_attempt_at,
                    now,
                    alert_id,
                ),
            )
            if cursor.rowcount == 0:
                return None
            row = conn.execute(
                "SELECT * FROM project_job_batch_alerts WHERE id = ?",
                (alert_id,),
            ).fetchone()
        return self._row_to_project_job_batch_alert(row) if row is not None else None

    def retry_failed_jobs(
        self,
        *,
        project_id: str,
        batch_id: str | None = None,
    ) -> int:
        now = _utc_now()
        params: list[str] = [JobStatus.QUEUED.value, now, project_id, JobStatus.FAILED.value]
        where = "project_id = ? AND status = ?"
        if batch_id is not None:
            where += " AND batch_id = ?"
            params.append(batch_id)

        with self._connect() as conn:
            cursor = conn.execute(
                f"""
                UPDATE jobs
                   SET status = ?,
                       claimed_by = NULL,
                       claim_token = NULL,
                       claimed_at = NULL,
                       claim_expires_at = NULL,
                       error = NULL,
                       failure_category = NULL,
                       updated_at = ?
                 WHERE {where}
                """,
                params,
            )
        return cursor.rowcount

    def _ensure_parent(self) -> None:
        if self.db_path == ":memory:" or self.db_path.startswith("file:"):
            return
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.db_path,
            uri=self.db_path.startswith("file:"),
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _migrate(self, conn: sqlite3.Connection) -> None:
        job_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(jobs)").fetchall()
        }
        for column, definition in {
            "source_asset_id": "TEXT",
            "claimed_by": "TEXT",
            "claim_token": "TEXT",
            "claimed_at": "TEXT",
            "claim_expires_at": "TEXT",
            "project_id": "TEXT",
            "batch_id": "TEXT",
            "failure_category": "TEXT",
            "idempotency_key": "TEXT",
        }.items():
            if column not in job_columns:
                conn.execute(f"ALTER TABLE jobs ADD COLUMN {column} {definition}")

        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_idempotency_key
              ON jobs(idempotency_key) WHERE idempotency_key IS NOT NULL
            """
        )

        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_jobs_source_asset ON jobs(source_asset_id)"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_project ON jobs(project_id)")
        output_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(job_outputs)").fetchall()
        }
        for column, definition in {
            "review_status": "TEXT NOT NULL DEFAULT 'pending_review'",
            "review_note": "TEXT",
            "reviewed_at": "TEXT",
            "cost_usd": "REAL NOT NULL DEFAULT 0",
            "cost_ticks": "INTEGER",
            "model": "TEXT",
            "latency_ms": "INTEGER",
            "render_request_id": "TEXT",
            "rights_confirmed_at": "TEXT",
            "rights_confirmed_by": "TEXT",
            "license_note": "TEXT",
        }.items():
            if column not in output_columns:
                conn.execute(f"ALTER TABLE job_outputs ADD COLUMN {column} {definition}")

        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_jobs_claim ON jobs(status, claim_expires_at)"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_batch ON jobs(batch_id)")
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_project_job_batches_project
              ON project_job_batches(project_id, created_at)
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_job_outputs_review ON job_outputs(review_status)"
        )
        alert_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(project_job_batch_alerts)").fetchall()
        }
        for column, definition in {
            "delivery_attempt_count": "INTEGER NOT NULL DEFAULT 0",
            "next_delivery_attempt_at": "TEXT",
            "resolved_at": "TEXT",
        }.items():
            if column not in alert_columns:
                conn.execute(
                    f"ALTER TABLE project_job_batch_alerts ADD COLUMN {column} {definition}"
                )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_project_job_batch_alerts_batch
              ON project_job_batch_alerts(project_id, batch_id, level)
            """
        )

    @staticmethod
    def _row_to_project_job_batch_alert(row: sqlite3.Row) -> ProjectJobBatchAlertRecord:
        return ProjectJobBatchAlertRecord(
            id=row["id"],
            project_id=row["project_id"],
            batch_id=row["batch_id"],
            level=row["level"],
            code=row["code"],
            message=row["message"],
            count=row["count"],
            last_seen_at=row["last_seen_at"],
            delivered_at=row["delivered_at"],
            delivery_attempted_at=row["delivery_attempted_at"],
            delivery_error=row["delivery_error"],
            delivery_attempt_count=row["delivery_attempt_count"],
            next_delivery_attempt_at=row["next_delivery_attempt_at"],
            acknowledged_at=row["acknowledged_at"],
            acknowledged_by=row["acknowledged_by"],
            muted_until=row["muted_until"],
            resolved_at=row["resolved_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _row_to_client(row: sqlite3.Row) -> ClientRecord:
        return ClientRecord(
            id=row["id"],
            name=row["name"],
            external_id=row["external_id"],
            notes=row["notes"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _row_to_project(self, row: sqlite3.Row) -> ProjectRecord:
        return ProjectRecord(
            id=row["id"],
            client_id=row["client_id"],
            client=self.get_client(row["client_id"]),
            name=row["name"],
            external_id=row["external_id"],
            notes=row["notes"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _row_to_asset(row: sqlite3.Row) -> AssetRecord:
        return AssetRecord(
            id=row["id"],
            original_filename=row["original_filename"],
            content_type=row["content_type"],
            storage_path=row["storage_path"],
            bytes=row["bytes"],
            sha256=row["sha256"],
            width=row["width"],
            height=row["height"],
            created_at=row["created_at"],
        )

    @staticmethod
    def _row_to_output(row: sqlite3.Row) -> JobOutputRecord:
        return JobOutputRecord(
            id=row["id"],
            job_id=row["job_id"],
            variant_id=row["variant_id"],
            status=OutputStatus(row["status"]),
            storage_path=row["storage_path"],
            content_type=row["content_type"],
            bytes=row["bytes"],
            sha256=row["sha256"],
            width=row["width"],
            height=row["height"],
            cost_usd=row["cost_usd"] if row["cost_usd"] is not None else 0.0,
            cost_ticks=row["cost_ticks"],
            model=row["model"],
            latency_ms=row["latency_ms"],
            render_request_id=row["render_request_id"],
            error=row["error"],
            review_status=OutputReviewStatus(row["review_status"]),
            review_note=row["review_note"],
            reviewed_at=row["reviewed_at"],
            rights_confirmed_at=row["rights_confirmed_at"],
            rights_confirmed_by=row["rights_confirmed_by"],
            license_note=row["license_note"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _row_to_project_job_batch(self, row: sqlite3.Row) -> ProjectJobBatchRecord:
        jobs = self.list_jobs(project_id=row["project_id"], batch_id=row["id"], limit=100)
        return ProjectJobBatchRecord(
            id=row["id"],
            project_id=row["project_id"],
            source=row["source"],
            created=row["item_count"],
            jobs=jobs,
            created_at=row["created_at"],
        )

    def _row_to_job(self, row: sqlite3.Row) -> JobRecord:
        payload = json.loads(row["payload_json"])
        source_asset_id = row["source_asset_id"]
        project_id = row["project_id"]
        output_plan = [
            OutputVariant(**variant)
            for variant in json.loads(row["output_plan_json"])
        ]
        return JobRecord(
            id=row["id"],
            status=JobStatus(row["status"]),
            product=ProductInput(**payload["product"]),
            source_asset_id=source_asset_id,
            source_asset=self.get_asset(source_asset_id) if source_asset_id is not None else None,
            project_id=project_id,
            project=self.get_project(project_id) if project_id is not None else None,
            batch_id=row["batch_id"],
            marketplace_targets=payload["marketplace_targets"],
            output_plan=output_plan,
            outputs=self.list_job_outputs(row["id"]),
            priority=row["priority"],
            idempotency_key=row["idempotency_key"],
            claimed_by=row["claimed_by"],
            claimed_at=row["claimed_at"],
            claim_expires_at=row["claim_expires_at"],
            error=row["error"],
            failure_category=(
                JobFailureCategory(row["failure_category"])
                if row["failure_category"] is not None
                else None
            ),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _get_output(self, *, job_id: str, variant_id: str) -> JobOutputRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM job_outputs WHERE job_id = ? AND variant_id = ?",
                (job_id, variant_id),
            ).fetchone()
        return self._row_to_output(row) if row is not None else None

    @staticmethod
    def _has_active_claim(row: sqlite3.Row, claim_token: str, now: str) -> bool:
        return (
            row["status"] == JobStatus.RENDERING.value
            and row["claim_token"] == claim_token
            and row["claim_expires_at"] is not None
            and row["claim_expires_at"] > now
        )
