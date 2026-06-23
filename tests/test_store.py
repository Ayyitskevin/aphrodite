import json
from pathlib import Path

import pytest

from aphrodite.domain import JobCreate, JobStatus, ProductInput
from aphrodite.store import AssetNotFoundError, JobStore


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
    assert legacy.product.source_image_uri == "file:///legacy/wallet.jpg"


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
