from pathlib import Path

from aphrodite.domain import JobCreate, JobStatus, ProductInput
from aphrodite.store import JobStore


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


def test_store_lists_and_updates_jobs(tmp_path: Path) -> None:
    store = JobStore(str(tmp_path / "aphrodite.db"))
    store.initialize()
    created = store.create_job(request())

    updated = store.update_status(created.id, JobStatus.RENDERING)

    assert updated is not None
    assert updated.status == JobStatus.RENDERING
    assert [job.id for job in store.list_jobs(status=JobStatus.RENDERING)] == [created.id]
    assert store.list_jobs(status=JobStatus.COMPLETED) == []
