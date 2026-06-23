"""CSV catalog import helpers for project-owned job batches."""

from __future__ import annotations

import csv
import re
from io import StringIO

from pydantic import ValidationError

from aphrodite.domain import (
    BackgroundIntent,
    ProductInput,
    ProjectJobBatchCreate,
    ProjectJobBatchItem,
)

CATALOG_CSV_COLUMNS = [
    "name",
    "sku",
    "category",
    "instructions",
    "source_image_uri",
    "source_asset_id",
    "marketplace_targets",
    "background_style",
    "background_prompt",
    "quantity_per_target",
    "priority",
]

_HEADER_ALIASES = {
    "asset_id": "source_asset_id",
    "background": "background_style",
    "image_uri": "source_image_uri",
    "marketplaces": "marketplace_targets",
    "product_name": "name",
    "prompt": "background_prompt",
    "qty": "quantity_per_target",
    "quantity": "quantity_per_target",
    "source_uri": "source_image_uri",
    "target": "marketplace_targets",
    "targets": "marketplace_targets",
}
_ALLOWED_COLUMNS = set(CATALOG_CSV_COLUMNS)


class CatalogImportError(ValueError):
    """Raised when a catalog CSV cannot be converted into a batch request."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def catalog_csv_template() -> str:
    buffer = StringIO()
    writer = csv.DictWriter(buffer, fieldnames=CATALOG_CSV_COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerow(
        {
            "name": "Matte ceramic mug",
            "sku": "MUG-001",
            "category": "Drinkware",
            "instructions": "Keep handle visible",
            "source_image_uri": "file:///media/mug/source.jpg",
            "marketplace_targets": "catalog_square,transparent_cutout",
            "background_style": "studio_shadow",
            "quantity_per_target": "1",
            "priority": "6",
        }
    )
    writer.writerow(
        {
            "name": "Canvas tote",
            "sku": "TOTE-001",
            "category": "Bags",
            "source_image_uri": "file:///media/tote/source.jpg",
            "marketplace_targets": "social_square",
        }
    )
    return buffer.getvalue()


def split_marketplace_targets(value: str) -> list[str]:
    return [part.strip() for part in re.split(r"[,;|\n]+", value) if part.strip()]


def parse_catalog_csv(
    content: bytes,
    *,
    marketplace_targets: list[str],
    background: BackgroundIntent,
    quantity_per_target: int,
    priority: int,
) -> ProjectJobBatchCreate:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CatalogImportError("CSV must be UTF-8 encoded") from exc

    if not text.strip():
        raise CatalogImportError("CSV is empty")

    reader = csv.DictReader(StringIO(text))
    _normalize_reader_headers(reader)
    items: list[ProjectJobBatchItem] = []

    for row in reader:
        line_number = reader.line_num
        extra_values = row.pop(None, None)
        if extra_values and any(_clean(value) is not None for value in extra_values):
            raise CatalogImportError(f"row {line_number}: too many columns")

        cleaned = {key: _clean(value) for key, value in row.items() if key is not None}
        if all(value is None for value in cleaned.values()):
            continue

        name = cleaned.get("name")
        if name is None:
            raise CatalogImportError(f"row {line_number}: name is required")

        try:
            item = ProjectJobBatchItem(
                product=ProductInput(
                    name=name,
                    sku=cleaned.get("sku"),
                    category=cleaned.get("category"),
                    instructions=cleaned.get("instructions"),
                    source_image_uri=cleaned.get("source_image_uri"),
                ),
                source_asset_id=cleaned.get("source_asset_id"),
                marketplace_targets=_row_marketplace_targets(cleaned),
                background=_row_background(cleaned, fallback=background),
                quantity_per_target=cleaned.get("quantity_per_target"),
                priority=cleaned.get("priority"),
            )
        except ValidationError as exc:
            raise CatalogImportError(
                f"row {line_number}: {_first_validation_error(exc)}"
            ) from exc

        items.append(item)
        if len(items) > 100:
            raise CatalogImportError("CSV contains more than 100 product rows")

    if not items:
        raise CatalogImportError("CSV must include at least one product row")

    try:
        return ProjectJobBatchCreate(
            marketplace_targets=marketplace_targets,
            background=background,
            quantity_per_target=quantity_per_target,
            priority=priority,
            items=items,
        )
    except ValidationError as exc:
        raise CatalogImportError(_first_validation_error(exc)) from exc


def _normalize_reader_headers(reader: csv.DictReader[str]) -> None:
    if reader.fieldnames is None:
        raise CatalogImportError("CSV header row is required")

    canonical_headers: list[str] = []
    duplicates: list[str] = []
    unknown: list[str] = []
    seen: set[str] = set()

    for raw_header in reader.fieldnames:
        header = _canonical_header(raw_header)
        if not header:
            raise CatalogImportError("CSV contains a blank column header")
        if header in seen:
            duplicates.append(header)
        seen.add(header)
        if header not in _ALLOWED_COLUMNS:
            unknown.append(header)
        canonical_headers.append(header)

    if duplicates:
        raise CatalogImportError(f"duplicate column(s): {', '.join(sorted(set(duplicates)))}")
    if unknown:
        raise CatalogImportError(f"unknown column(s): {', '.join(sorted(set(unknown)))}")
    if "name" not in seen:
        raise CatalogImportError("CSV header must include name")

    reader.fieldnames = canonical_headers


def _canonical_header(value: str | None) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", (value or "").strip().lower()).strip("_")
    return _HEADER_ALIASES.get(normalized, normalized)


def _row_marketplace_targets(row: dict[str, str | None]) -> list[str] | None:
    value = row.get("marketplace_targets")
    if value is None:
        return None
    return split_marketplace_targets(value)


def _row_background(
    row: dict[str, str | None],
    *,
    fallback: BackgroundIntent,
) -> BackgroundIntent | None:
    style = row.get("background_style")
    prompt = row.get("background_prompt")
    if style is None and prompt is None:
        return None
    return BackgroundIntent(style=style or fallback.style, prompt=prompt)


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def _first_validation_error(exc: ValidationError) -> str:
    error = exc.errors()[0]
    location = ".".join(str(part) for part in error.get("loc", ()))
    message = str(error.get("msg", "invalid value"))
    return f"{location}: {message}" if location else message
