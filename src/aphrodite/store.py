"""SQLite persistence for Aphrodite assets and jobs."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel

from aphrodite.domain import (
    AssetRecord,
    JobCreate,
    JobRecord,
    JobStatus,
    OutputVariant,
    ProductInput,
    build_output_plan,
)

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

CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  status TEXT NOT NULL,
  product_name TEXT NOT NULL,
  product_sku TEXT,
  source_image_uri TEXT,
  source_asset_id TEXT,
  payload_json TEXT NOT NULL,
  output_plan_json TEXT NOT NULL,
  priority INTEGER NOT NULL,
  error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(source_asset_id) REFERENCES assets(id)
);

CREATE INDEX IF NOT EXISTS idx_jobs_status_updated
  ON jobs(status, updated_at);
"""


class AssetNotFoundError(Exception):
    def __init__(self, asset_id: str) -> None:
        super().__init__(f"asset not found: {asset_id}")
        self.asset_id = asset_id


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _jsonable(model: BaseModel) -> dict:
    return model.model_dump(mode="json")


class JobStore:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

    def initialize(self) -> None:
        self._ensure_parent()
        with self._connect() as conn:
            if self.db_path != ":memory:":
                conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
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
    ) -> AssetRecord:
        asset = AssetRecord(
            id=str(uuid.uuid4()),
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

    def create_job(self, request: JobCreate) -> JobRecord:
        source_asset = None
        if request.source_asset_id is not None:
            source_asset = self.get_asset(request.source_asset_id)
            if source_asset is None:
                raise AssetNotFoundError(request.source_asset_id)

        output_plan = build_output_plan(request)
        now = _utc_now()
        job = JobRecord(
            id=str(uuid.uuid4()),
            status=JobStatus.QUEUED,
            product=request.product,
            source_asset_id=request.source_asset_id,
            source_asset=source_asset,
            marketplace_targets=request.marketplace_targets,
            output_plan=output_plan,
            priority=request.priority,
            created_at=now,
            updated_at=now,
        )
        source_image_uri = request.product.source_image_uri or f"asset://{request.source_asset_id}"

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO jobs (
                  id, status, product_name, product_sku, source_image_uri, source_asset_id,
                  payload_json, output_plan_json, priority, error, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job.id,
                    job.status.value,
                    request.product.name,
                    request.product.sku,
                    source_image_uri,
                    request.source_asset_id,
                    json.dumps(_jsonable(request), sort_keys=True),
                    json.dumps([_jsonable(variant) for variant in output_plan], sort_keys=True),
                    request.priority,
                    None,
                    now,
                    now,
                ),
            )

        return job

    def get_job(self, job_id: str) -> JobRecord | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return self._row_to_job(row) if row is not None else None

    def list_jobs(self, *, status: JobStatus | None = None, limit: int = 50) -> list[JobRecord]:
        bounded_limit = max(1, min(limit, 100))

        with self._connect() as conn:
            if status is None:
                rows = conn.execute(
                    "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?",
                    (bounded_limit,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM jobs WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                    (status.value, bounded_limit),
                ).fetchall()

        return [self._row_to_job(row) for row in rows]

    def update_status(
        self,
        job_id: str,
        status: JobStatus,
        *,
        error: str | None = None,
    ) -> JobRecord | None:
        now = _utc_now()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE jobs
                   SET status = ?, error = ?, updated_at = ?
                 WHERE id = ?
                """,
                (status.value, error, now, job_id),
            )
            if cursor.rowcount == 0:
                return None

        return self.get_job(job_id)

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
        return conn

    def _migrate(self, conn: sqlite3.Connection) -> None:
        job_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(jobs)").fetchall()
        }
        if "source_asset_id" not in job_columns:
            conn.execute("ALTER TABLE jobs ADD COLUMN source_asset_id TEXT")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_jobs_source_asset ON jobs(source_asset_id)"
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

    def _row_to_job(self, row: sqlite3.Row) -> JobRecord:
        payload = json.loads(row["payload_json"])
        source_asset_id = row["source_asset_id"]
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
            marketplace_targets=payload["marketplace_targets"],
            output_plan=output_plan,
            priority=row["priority"],
            error=row["error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
