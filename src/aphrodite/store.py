"""SQLite persistence for Aphrodite jobs."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel

from aphrodite.domain import (
    JobCreate,
    JobRecord,
    JobStatus,
    OutputVariant,
    ProductInput,
    build_output_plan,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  status TEXT NOT NULL,
  product_name TEXT NOT NULL,
  product_sku TEXT,
  source_image_uri TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  output_plan_json TEXT NOT NULL,
  priority INTEGER NOT NULL,
  error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_jobs_status_updated
  ON jobs(status, updated_at);
"""


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
            conn.executescript(SCHEMA)

    def ping(self) -> bool:
        try:
            with self._connect() as conn:
                conn.execute("SELECT 1").fetchone()
        except sqlite3.Error:
            return False
        return True

    def create_job(self, request: JobCreate) -> JobRecord:
        output_plan = build_output_plan(request)
        now = _utc_now()
        job = JobRecord(
            id=str(uuid.uuid4()),
            status=JobStatus.QUEUED,
            product=request.product,
            marketplace_targets=request.marketplace_targets,
            output_plan=output_plan,
            priority=request.priority,
            created_at=now,
            updated_at=now,
        )

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO jobs (
                  id, status, product_name, product_sku, source_image_uri,
                  payload_json, output_plan_json, priority, error, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job.id,
                    job.status.value,
                    request.product.name,
                    request.product.sku,
                    request.product.source_image_uri,
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

    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> JobRecord:
        payload = json.loads(row["payload_json"])
        output_plan = [
            OutputVariant(**variant)
            for variant in json.loads(row["output_plan_json"])
        ]
        return JobRecord(
            id=row["id"],
            status=JobStatus(row["status"]),
            product=ProductInput(**payload["product"]),
            marketplace_targets=payload["marketplace_targets"],
            output_plan=output_plan,
            priority=row["priority"],
            error=row["error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
