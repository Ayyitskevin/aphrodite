import pytest
from pydantic import ValidationError

from aphrodite.domain import JobCreate, ProductInput, build_output_plan


def product() -> ProductInput:
    return ProductInput(
        name="Matte ceramic mug",
        sku="MUG-001",
        source_image_uri="file:///media/mug/source.jpg",
    )


def test_build_output_plan_expands_targets_and_quantities() -> None:
    request = JobCreate(
        product=product(),
        marketplace_targets=["catalog_square", "social_square"],
        quantity_per_target=2,
        background={"style": "lifestyle", "prompt": "morning kitchen counter"},
    )

    plan = build_output_plan(request)

    assert [variant.id for variant in plan] == [
        "catalog_square-1",
        "catalog_square-2",
        "social_square-1",
        "social_square-2",
    ]
    assert plan[0].background == "clean_white"
    assert plan[2].background == "lifestyle"
    assert plan[2].prompt == "morning kitchen counter"


def test_unknown_marketplace_target_is_rejected() -> None:
    with pytest.raises(ValidationError):
        JobCreate(
            product=product(),
            marketplace_targets=["not_real"],
        )


def test_marketplace_targets_are_deduped() -> None:
    request = JobCreate(
        product=product(),
        marketplace_targets=["catalog_square", "catalog_square"],
    )

    assert request.marketplace_targets == ["catalog_square"]


def test_job_create_accepts_source_asset_without_source_uri() -> None:
    request = JobCreate(
        product=ProductInput(name="Matte ceramic mug"),
        source_asset_id="asset-123",
    )

    assert request.source_asset_id == "asset-123"
    assert request.product.source_image_uri is None


def test_job_create_requires_some_source() -> None:
    with pytest.raises(ValidationError):
        JobCreate(product=ProductInput(name="Matte ceramic mug"))
