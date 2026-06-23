import pytest

from aphrodite.catalog_import import (
    CatalogImportError,
    catalog_csv_template,
    parse_catalog_csv,
    split_marketplace_targets,
)
from aphrodite.domain import BackgroundIntent


def test_parse_catalog_csv_builds_batch_with_defaults_and_overrides() -> None:
    content = (
        "\ufeffproduct_name,sku,source_uri,targets,background,prompt,quantity,priority\n"
        "Matte mug,MUG-001,file:///media/mug.jpg,,studio_shadow,soft shadow,2,7\n"
        "Canvas tote,TOTE-001,file:///media/tote.jpg,social_square,,,1,9\n"
    ).encode()

    batch = parse_catalog_csv(
        content,
        marketplace_targets=["catalog_square", "transparent_cutout"],
        background=BackgroundIntent(style="clean_white"),
        quantity_per_target=1,
        priority=5,
    )

    assert batch.marketplace_targets == ["catalog_square", "transparent_cutout"]
    assert len(batch.items) == 2
    assert batch.items[0].product.name == "Matte mug"
    assert batch.items[0].background is not None
    assert batch.items[0].background.style == "studio_shadow"
    assert batch.items[0].background.prompt == "soft shadow"
    assert batch.items[0].quantity_per_target == 2
    assert batch.items[0].priority == 7
    assert batch.items[1].marketplace_targets == ["social_square"]
    assert batch.items[1].priority == 9


def test_parse_catalog_csv_rejects_bad_rows_with_line_numbers() -> None:
    with pytest.raises(CatalogImportError, match="row 2: name is required"):
        parse_catalog_csv(
            b"name,source_image_uri\n,file:///media/missing-name.jpg\n",
            marketplace_targets=["catalog_square"],
            background=BackgroundIntent(),
            quantity_per_target=1,
            priority=5,
        )


def test_parse_catalog_csv_rejects_unknown_columns() -> None:
    with pytest.raises(CatalogImportError, match="unknown column"):
        parse_catalog_csv(
            b"name,unexpected,source_image_uri\nMug,value,file:///media/mug.jpg\n",
            marketplace_targets=["catalog_square"],
            background=BackgroundIntent(),
            quantity_per_target=1,
            priority=5,
        )


def test_split_marketplace_targets_accepts_sheet_friendly_separators() -> None:
    assert split_marketplace_targets("catalog_square; transparent_cutout|social_square") == [
        "catalog_square",
        "transparent_cutout",
        "social_square",
    ]


def test_catalog_csv_template_contains_import_columns() -> None:
    template = catalog_csv_template()

    assert template.startswith("name,sku,category,instructions,source_image_uri")
    assert "Matte ceramic mug" in template
