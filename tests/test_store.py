import json
from pathlib import Path

import pytest

from aphrodite.domain import (
    ClientCreate,
    JobCreate,
    JobFailureCategory,
    JobStatus,
    ProductInput,
    ProjectCreate,
    ProjectJobBatchCreate,
    ProjectJobBatchItem,
)
from aphrodite.store import AssetNotFoundError, JobStore, ProjectNotFoundError


def request() -> JobCreate:
    return JobCreate(
        product=ProductInput(
            name="Leather wallet",
            sku="WALLET-001",
            source_image_uri="file:///media/wallet/source.jpg",
        ),
        marketplace_targets=["catalog_square", "transparent_cutout"],
        priority=7,
    )


def test_store_migrates_foundation_jobs_table(tmp_path: Path) -> None:
    db_path = tmp_path / "aphrodite.db"
    payload = {
        "product": {
            "name": "Legacy wallet",
            "source_image_uri": "file:///legacy/wallet.jpg",
        },
        "marketplace_targets": ["catalog_square"],
        "background": {"style": "clean_white", "prompt": None},
        "quantity_per_target": 1,
        "priority": 5,
    }
    output_plan = [
        {
            "id": "catalog_square",
            "target_id": "catalog_square",
            "label": "Catalog square packshot",
            "width": 2000,
            "height": 2000,
            "aspect_ratio": "1:1",
            "output_format": "jpg",
            "background": "clean_white",
            "prompt": None,
            "safe_margin_percent": 8,
        }
    ]

    import sqlite3

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE jobs (
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
            )
            """
        )
        conn.execute(
            """
            INSERT INTO jobs (
              id, status, product_name, product_sku, source_image_uri,
              payload_json, output_plan_json, priority, error, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "legacy-job",
                "queued",
                "Legacy wallet",
                None,
                "file:///legacy/wallet.jpg",
                json.dumps(payload),
                json.dumps(output_plan),
                5,
                None,
                "2026-06-23T00:00:00Z",
                "2026-06-23T00:00:00Z",
            ),
        )

    store = JobStore(str(db_path))
    store.initialize()

    legacy = store.get_job("legacy-job")
    assert legacy is not None
    assert legacy.source_asset_id is None
    assert legacy.project_id is None
    assert legacy.batch_id is None
    assert legacy.product.source_image_uri == "file:///legacy/wallet.jpg"

    with sqlite3.connect(db_path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
        table_rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        tables = {row[0] for row in table_rows}
    assert "project_id" in columns
    assert "batch_id" in columns
    assert "failure_category" in columns
    assert "project_job_batches" in tables
    assert "project_job_batch_alerts" in tables


def test_store_migrates_output_review_columns(tmp_path: Path) -> None:
    db_path = tmp_path / "aphrodite.db"
    import sqlite3

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE job_outputs (
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
              error TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              UNIQUE(job_id, variant_id)
            )
            """
        )

    store = JobStore(str(db_path))
    store.initialize()

    with sqlite3.connect(db_path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(job_outputs)").fetchall()}
    assert {"review_status", "review_note", "reviewed_at"}.issubset(columns)


def test_store_creates_clients_projects_and_filters_owned_jobs(tmp_path: Path) -> None:
    store = JobStore(str(tmp_path / "aphrodite.db"))
    store.initialize()
    client = store.create_client(ClientCreate(name="Maison Test", external_id="client-001"))
    project = store.create_project(
        ProjectCreate(client_id=client.id, name="Spring catalog", external_id="project-001")
    )
    other_client = store.create_client(ClientCreate(name="Other Client"))
    other_project = store.create_project(
        ProjectCreate(client_id=other_client.id, name="Other catalog")
    )

    created = store.create_job(
        JobCreate(
            product=ProductInput(
                name="Leather wallet",
                source_image_uri="file:///media/wallet/source.jpg",
            ),
            project_id=project.id,
            marketplace_targets=["catalog_square"],
        )
    )
    store.create_job(
        JobCreate(
            product=ProductInput(
                name="Other wallet",
                source_image_uri="file:///media/other/source.jpg",
            ),
            project_id=other_project.id,
            marketplace_targets=["catalog_square"],
        )
    )

    loaded = store.get_job(created.id)

    assert loaded is not None
    assert loaded.project_id == project.id
    assert loaded.project is not None
    assert loaded.project.name == "Spring catalog"
    assert loaded.project.client is not None
    assert loaded.project.client.name == "Maison Test"
    assert [job.id for job in store.list_jobs(project_id=project.id)] == [created.id]
    assert [job.id for job in store.list_jobs(client_id=client.id)] == [created.id]
    assert store.list_projects(client_id=client.id) == [project]
    assert store.list_clients(limit=1) == [other_client]


def test_store_creates_project_job_batch_atomically(tmp_path: Path) -> None:
    store = JobStore(str(tmp_path / "aphrodite.db"))
    store.initialize()
    client = store.create_client(ClientCreate(name="Batch Client"))
    project = store.create_project(ProjectCreate(client_id=client.id, name="Batch Catalog"))

    created = store.create_project_job_batch(
        project_id=project.id,
        request=ProjectJobBatchCreate(
            marketplace_targets=["catalog_square", "transparent_cutout"],
            items=[
                ProjectJobBatchItem(
                    product=ProductInput(
                        name="Batch wallet",
                        source_image_uri="file:///media/wallet.jpg",
                    )
                ),
                ProjectJobBatchItem(
                    product=ProductInput(
                        name="Batch tote",
                        source_image_uri="file:///media/tote.jpg",
                    ),
                    marketplace_targets=["social_square"],
                    priority=9,
                ),
            ],
        ),
    )

    assert len(created) == 2
    assert all(job.project_id == project.id for job in created)
    assert all(job.batch_id == created[0].batch_id for job in created)
    assert created[0].batch_id is not None
    assert created[0].project is not None
    assert created[0].project.client is not None
    assert created[0].project.client.name == "Batch Client"
    assert [variant.target_id for variant in created[0].output_plan] == [
        "catalog_square",
        "transparent_cutout",
    ]
    assert [variant.target_id for variant in created[1].output_plan] == ["social_square"]
    assert created[1].priority == 9

    batches = store.list_project_job_batches(project_id=project.id)
    assert len(batches) == 1
    assert batches[0].id == created[0].batch_id
    assert batches[0].source == "api"
    assert batches[0].created == 2
    assert {job.id for job in batches[0].jobs} == {created[0].id, created[1].id}
    assert store.get_project_job_batch(batches[0].id) == batches[0]

    with pytest.raises(AssetNotFoundError):
        store.create_project_job_batch(
            project_id=project.id,
            request=ProjectJobBatchCreate(
                items=[
                    ProjectJobBatchItem(
                        product=ProductInput(
                            name="Valid item",
                            source_image_uri="file:///media/valid.jpg",
                        )
                    ),
                    ProjectJobBatchItem(
                        product=ProductInput(name="Missing asset"),
                        source_asset_id="missing",
                    ),
                ],
            ),
        )
    assert len(store.list_jobs(project_id=project.id)) == 2


def test_store_retries_failed_project_and_batch_jobs(tmp_path: Path) -> None:
    store = JobStore(str(tmp_path / "aphrodite.db"))
    store.initialize()
    client = store.create_client(ClientCreate(name="Retry Client"))
    project = store.create_project(ProjectCreate(client_id=client.id, name="Retry Catalog"))
    created = store.create_project_job_batch(
        project_id=project.id,
        request=ProjectJobBatchCreate(
            items=[
                ProjectJobBatchItem(
                    product=ProductInput(
                        name="Retry failed",
                        source_image_uri="file:///media/retry-failed.jpg",
                    )
                ),
                ProjectJobBatchItem(
                    product=ProductInput(
                        name="Retry queued",
                        source_image_uri="file:///media/retry-queued.jpg",
                    )
                ),
            ],
        ),
        source="admin_csv",
    )
    failed = store.update_status(created[0].id, JobStatus.FAILED, error="renderer crashed")
    assert failed is not None
    assert failed.failure_category == JobFailureCategory.RENDERER_ERROR

    retried = store.retry_failed_jobs(project_id=project.id, batch_id=created[0].batch_id)

    assert retried == 1
    requeued = store.get_job(created[0].id)
    untouched = store.get_job(created[1].id)
    assert requeued is not None
    assert requeued.status == JobStatus.QUEUED
    assert requeued.error is None
    assert requeued.failure_category is None
    assert untouched is not None
    assert untouched.status == JobStatus.QUEUED


def test_store_project_job_batch_rejects_missing_project(tmp_path: Path) -> None:
    store = JobStore(str(tmp_path / "aphrodite.db"))
    store.initialize()

    with pytest.raises(ProjectNotFoundError):
        store.create_project_job_batch(
            project_id="missing",
            request=ProjectJobBatchCreate(
                items=[
                    ProjectJobBatchItem(
                        product=ProductInput(
                            name="Batch item",
                            source_image_uri="file:///media/item.jpg",
                        )
                    )
                ]
            ),
        )


def test_store_creates_and_loads_assets(tmp_path: Path) -> None:
    store = JobStore(str(tmp_path / "aphrodite.db"))
    store.initialize()

    created = store.create_asset(
        original_filename="wallet.png",
        content_type="image/png",
        storage_path="originals/wallet.png",
        bytes=42,
        sha256="abc123",
        width=100,
        height=120,
    )
    loaded = store.get_asset(created.id)

    assert loaded == created
    assert store.list_assets() == [created]


def test_store_creates_and_loads_jobs(tmp_path: Path) -> None:
    store = JobStore(str(tmp_path / "aphrodite.db"))
    store.initialize()

    created = store.create_job(request())
    loaded = store.get_job(created.id)

    assert loaded is not None
    assert loaded.id == created.id
    assert loaded.status == JobStatus.QUEUED
    assert loaded.product.name == "Leather wallet"
    assert loaded.priority == 7
    assert [variant.target_id for variant in loaded.output_plan] == [
        "catalog_square",
        "transparent_cutout",
    ]


def test_store_creates_job_from_asset(tmp_path: Path) -> None:
    store = JobStore(str(tmp_path / "aphrodite.db"))
    store.initialize()
    asset = store.create_asset(
        original_filename="wallet.png",
        content_type="image/png",
        storage_path="originals/wallet.png",
        bytes=42,
        sha256="abc123",
        width=100,
        height=120,
    )

    created = store.create_job(
        JobCreate(
            product=ProductInput(name="Leather wallet", sku="WALLET-001"),
            source_asset_id=asset.id,
            marketplace_targets=["catalog_square"],
        )
    )
    loaded = store.get_job(created.id)

    assert created.source_asset_id == asset.id
    assert created.source_asset == asset
    assert created.product.source_image_uri is None
    assert loaded is not None
    assert loaded.source_asset == asset


def test_store_rejects_missing_asset_job(tmp_path: Path) -> None:
    store = JobStore(str(tmp_path / "aphrodite.db"))
    store.initialize()

    with pytest.raises(AssetNotFoundError):
        store.create_job(
            JobCreate(
                product=ProductInput(name="Leather wallet"),
                source_asset_id="missing",
            )
        )


def test_store_lists_and_updates_jobs(tmp_path: Path) -> None:
    store = JobStore(str(tmp_path / "aphrodite.db"))
    store.initialize()
    created = store.create_job(request())

    updated = store.update_status(created.id, JobStatus.RENDERING)

    assert updated is not None
    assert updated.status == JobStatus.RENDERING
    assert [job.id for job in store.list_jobs(status=JobStatus.RENDERING)] == [created.id]
    assert store.list_jobs(status=JobStatus.COMPLETED) == []
