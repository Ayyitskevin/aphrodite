"""Starter output preset registry for Aphrodite jobs."""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True, slots=True)
class MarketplaceSpec:
    id: str
    label: str
    width: int
    height: int
    aspect_ratio: str
    background: str
    output_format: str
    safe_margin_percent: int
    notes: str

    def as_dict(self) -> dict[str, str | int]:
        return asdict(self)


MARKETPLACE_SPECS: dict[str, MarketplaceSpec] = {
    "catalog_square": MarketplaceSpec(
        id="catalog_square",
        label="Catalog square packshot",
        width=2000,
        height=2000,
        aspect_ratio="1:1",
        background="clean_white",
        output_format="jpg",
        safe_margin_percent=8,
        notes="Neutral catalog image for product grids and primary commerce listings.",
    ),
    "marketplace_main": MarketplaceSpec(
        id="marketplace_main",
        label="Marketplace main image",
        width=1600,
        height=1600,
        aspect_ratio="1:1",
        background="clean_white",
        output_format="jpg",
        safe_margin_percent=10,
        notes="Conservative main image preset for broad marketplace compatibility.",
    ),
    "transparent_cutout": MarketplaceSpec(
        id="transparent_cutout",
        label="Transparent product cutout",
        width=2000,
        height=2000,
        aspect_ratio="1:1",
        background="transparent",
        output_format="png",
        safe_margin_percent=8,
        notes="Transparent-background asset for compositing and downstream templates.",
    ),
    "social_square": MarketplaceSpec(
        id="social_square",
        label="Social commerce square",
        width=1080,
        height=1080,
        aspect_ratio="1:1",
        background="lifestyle",
        output_format="jpg",
        safe_margin_percent=12,
        notes="Compact lifestyle or branded square for social commerce surfaces.",
    ),
    "hero_wide": MarketplaceSpec(
        id="hero_wide",
        label="Wide hero product image",
        width=2400,
        height=1350,
        aspect_ratio="16:9",
        background="lifestyle",
        output_format="jpg",
        safe_margin_percent=15,
        notes="Wide merchandising image for landing pages, banners, and hero modules.",
    ),
}


def list_marketplace_specs() -> list[MarketplaceSpec]:
    return list(MARKETPLACE_SPECS.values())


def get_marketplace_spec(spec_id: str) -> MarketplaceSpec | None:
    return MARKETPLACE_SPECS.get(spec_id)
