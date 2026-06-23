"""Domain models and planning helpers for product photography jobs."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from aphrodite.marketplaces import MARKETPLACE_SPECS, get_marketplace_spec


class JobStatus(StrEnum):
    QUEUED = "queued"
    PLANNING = "planning"
    RENDERING = "rendering"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"


class AssetRecord(BaseModel):
    id: str
    original_filename: str
    content_type: str
    storage_path: str
    bytes: int
    sha256: str
    width: int
    height: int
    created_at: str


class ProductInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=160)
    source_image_uri: str | None = Field(default=None, min_length=1, max_length=2048)
    sku: str | None = Field(default=None, max_length=120)
    category: str | None = Field(default=None, max_length=160)
    instructions: str | None = Field(default=None, max_length=2000)

    @field_validator("source_image_uri")
    @classmethod
    def source_image_uri_must_be_actionable(cls, value: str | None) -> str | None:
        if value is not None and value.strip() == "":
            raise ValueError("source_image_uri cannot be blank")
        return value


class BackgroundIntent(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    style: Literal[
        "clean_white",
        "transparent",
        "studio_shadow",
        "lifestyle",
        "brand_gradient",
    ] = "clean_white"
    prompt: str | None = Field(default=None, max_length=1000)


class JobCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    product: ProductInput
    source_asset_id: str | None = Field(default=None, min_length=1, max_length=80)
    marketplace_targets: list[str] = Field(default_factory=lambda: ["catalog_square"])
    background: BackgroundIntent = Field(default_factory=BackgroundIntent)
    quantity_per_target: int = Field(default=1, ge=1, le=8)
    priority: int = Field(default=5, ge=0, le=10)

    @field_validator("marketplace_targets")
    @classmethod
    def marketplace_targets_must_be_known(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("at least one marketplace target is required")

        deduped = list(dict.fromkeys(value))
        unknown = sorted(target for target in deduped if target not in MARKETPLACE_SPECS)
        if unknown:
            known = ", ".join(sorted(MARKETPLACE_SPECS))
            raise ValueError(f"unknown marketplace target(s): {', '.join(unknown)}; known: {known}")
        return deduped

    @model_validator(mode="after")
    def source_must_be_known(self) -> JobCreate:
        if self.source_asset_id is None and self.product.source_image_uri is None:
            raise ValueError("source_asset_id or product.source_image_uri is required")
        return self


class OutputVariant(BaseModel):
    id: str
    target_id: str
    label: str
    width: int
    height: int
    aspect_ratio: str
    output_format: str
    background: str
    prompt: str | None = None
    safe_margin_percent: int


class JobRecord(BaseModel):
    id: str
    status: JobStatus
    product: ProductInput
    source_asset_id: str | None = None
    source_asset: AssetRecord | None = None
    marketplace_targets: list[str]
    output_plan: list[OutputVariant]
    priority: int
    error: str | None = None
    created_at: str
    updated_at: str


class JobStatusUpdate(BaseModel):
    status: JobStatus
    error: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def failed_jobs_need_error_message(self) -> JobStatusUpdate:
        if self.status == JobStatus.FAILED and not self.error:
            raise ValueError("error is required when status is failed")
        return self


def build_output_plan(request: JobCreate) -> list[OutputVariant]:
    variants: list[OutputVariant] = []

    for target_id in request.marketplace_targets:
        spec = get_marketplace_spec(target_id)
        if spec is None:
            raise ValueError(f"unknown marketplace target: {target_id}")

        background = (
            spec.background
            if spec.background in {"transparent", "clean_white"}
            else request.background.style
        )

        for index in range(1, request.quantity_per_target + 1):
            variant_id = target_id
            if request.quantity_per_target > 1:
                variant_id = f"{target_id}-{index}"

            variants.append(
                OutputVariant(
                    id=variant_id,
                    target_id=target_id,
                    label=spec.label,
                    width=spec.width,
                    height=spec.height,
                    aspect_ratio=spec.aspect_ratio,
                    output_format=spec.output_format,
                    background=background,
                    prompt=request.background.prompt,
                    safe_margin_percent=spec.safe_margin_percent,
                )
            )

    return variants
