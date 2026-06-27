"""Domain models and planning helpers for product photography jobs."""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from aphrodite.marketplaces import MARKETPLACE_SPECS, get_marketplace_spec


def _validated_marketplace_targets(value: list[str]) -> list[str]:
    if not value:
        raise ValueError("at least one marketplace target is required")

    deduped = list(dict.fromkeys(value))
    unknown = sorted(target for target in deduped if target not in MARKETPLACE_SPECS)
    if unknown:
        known = ", ".join(sorted(MARKETPLACE_SPECS))
        raise ValueError(f"unknown marketplace target(s): {', '.join(unknown)}; known: {known}")
    return deduped


class JobStatus(StrEnum):
    QUEUED = "queued"
    PLANNING = "planning"
    RENDERING = "rendering"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"


class JobFailureCategory(StrEnum):
    SOURCE_ASSET_ERROR = "source_asset_error"
    PROVIDER_ERROR = "provider_error"
    TIMEOUT = "timeout"
    BUDGET_EXCEEDED = "budget_exceeded"
    RENDERER_ERROR = "renderer_error"
    WORKER_ERROR = "worker_error"
    UNKNOWN = "unknown"


class OutputStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"


class OutputReviewStatus(StrEnum):
    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    REJECTED = "rejected"


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


class ClientCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=160)
    external_id: str | None = Field(default=None, max_length=160)
    notes: str | None = Field(default=None, max_length=2000)


class ClientRecord(ClientCreate):
    id: str
    created_at: str
    updated_at: str


class ProjectCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    client_id: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=160)
    external_id: str | None = Field(default=None, max_length=160)
    notes: str | None = Field(default=None, max_length=2000)


class ProjectRecord(ProjectCreate):
    id: str
    client: ClientRecord | None = None
    created_at: str
    updated_at: str


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
    project_id: str | None = Field(default=None, min_length=1, max_length=80)
    marketplace_targets: list[str] = Field(default_factory=lambda: ["catalog_square"])
    background: BackgroundIntent = Field(default_factory=BackgroundIntent)
    quantity_per_target: int = Field(default=1, ge=1, le=8)
    priority: int = Field(default=5, ge=0, le=10)
    # Optional caller-supplied request idempotency key. Re-submitting a create
    # with the same key returns the existing job instead of spawning a second
    # one, so a Mise retry cannot duplicate renders or double-charge.
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=200)

    @field_validator("marketplace_targets")
    @classmethod
    def marketplace_targets_must_be_known(cls, value: list[str]) -> list[str]:
        return _validated_marketplace_targets(value)

    @model_validator(mode="after")
    def source_must_be_known(self) -> JobCreate:
        if self.source_asset_id is None and self.product.source_image_uri is None:
            raise ValueError("source_asset_id or product.source_image_uri is required")
        return self


class ProjectJobBatchItem(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    product: ProductInput
    source_asset_id: str | None = Field(default=None, min_length=1, max_length=80)
    marketplace_targets: list[str] | None = None
    background: BackgroundIntent | None = None
    quantity_per_target: int | None = Field(default=None, ge=1, le=8)
    priority: int | None = Field(default=None, ge=0, le=10)

    @field_validator("marketplace_targets")
    @classmethod
    def marketplace_targets_must_be_known(cls, value: list[str] | None) -> list[str] | None:
        return _validated_marketplace_targets(value) if value is not None else None

    @model_validator(mode="after")
    def source_must_be_known(self) -> ProjectJobBatchItem:
        if self.source_asset_id is None and self.product.source_image_uri is None:
            raise ValueError("source_asset_id or product.source_image_uri is required")
        return self


class ProjectJobBatchCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    marketplace_targets: list[str] = Field(default_factory=lambda: ["catalog_square"])
    background: BackgroundIntent = Field(default_factory=BackgroundIntent)
    quantity_per_target: int = Field(default=1, ge=1, le=8)
    priority: int = Field(default=5, ge=0, le=10)
    items: list[ProjectJobBatchItem] = Field(min_length=1, max_length=100)

    @field_validator("marketplace_targets")
    @classmethod
    def marketplace_targets_must_be_known(cls, value: list[str]) -> list[str]:
        return _validated_marketplace_targets(value)


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


class JobOutputRecord(BaseModel):
    id: str
    job_id: str
    variant_id: str
    status: OutputStatus
    storage_path: str
    content_type: str
    bytes: int
    sha256: str
    width: int
    height: int
    # Real per-render spend + provenance. Mise sums cost_usd against its hard
    # cap and persists model/latency to its ledger. cost_usd defaults to 0.0 so
    # outputs created before the cost contract (and the free local_stub backend)
    # remain valid; cost_ticks/model/latency_ms stay null when unreported.
    cost_usd: float = Field(default=0.0, ge=0)
    cost_ticks: int | None = Field(default=None, ge=0)
    model: str | None = Field(default=None, max_length=200)
    latency_ms: int | None = Field(default=None, ge=0)
    render_request_id: str | None = Field(default=None, max_length=120)
    error: str | None = None
    review_status: OutputReviewStatus = OutputReviewStatus.PENDING_REVIEW
    review_note: str | None = None
    reviewed_at: str | None = None
    # Explicit rights/consent confirmation, distinct from quality approval. When
    # the consent policy is active, export requires this in addition to approval.
    # Reset whenever the output is regenerated so new media needs fresh consent.
    rights_confirmed_at: str | None = None
    rights_confirmed_by: str | None = None
    license_note: str | None = None
    created_at: str
    updated_at: str


class JobRecord(BaseModel):
    id: str
    status: JobStatus
    product: ProductInput
    source_asset_id: str | None = None
    source_asset: AssetRecord | None = None
    project_id: str | None = None
    project: ProjectRecord | None = None
    batch_id: str | None = None
    marketplace_targets: list[str]
    output_plan: list[OutputVariant]
    outputs: list[JobOutputRecord] = Field(default_factory=list)
    priority: int
    idempotency_key: str | None = None
    claimed_by: str | None = None
    claimed_at: str | None = None
    claim_expires_at: str | None = None
    error: str | None = None
    failure_category: JobFailureCategory | None = None
    created_at: str
    updated_at: str


class ProjectJobBatchRecord(BaseModel):
    id: str
    project_id: str
    source: str
    created: int
    jobs: list[JobRecord]
    created_at: str


class ProjectJobBatchStatusCounts(BaseModel):
    queued: int = 0
    planning: int = 0
    rendering: int = 0
    completed: int = 0
    failed: int = 0
    canceled: int = 0


class ProjectJobBatchReviewCounts(BaseModel):
    pending_review: int = 0
    approved: int = 0
    rejected: int = 0


class ProjectJobBatchFailureCounts(BaseModel):
    source_asset_error: int = 0
    provider_error: int = 0
    timeout: int = 0
    budget_exceeded: int = 0
    renderer_error: int = 0
    worker_error: int = 0
    unknown: int = 0


class ProjectJobBatchAlert(BaseModel):
    level: Literal["warning", "critical"]
    code: str
    message: str
    count: int = 0


class ProjectJobBatchAlertRecord(ProjectJobBatchAlert):
    id: str
    project_id: str
    batch_id: str
    last_seen_at: str
    delivered_at: str | None = None
    delivery_attempted_at: str | None = None
    delivery_error: str | None = None
    delivery_attempt_count: int = 0
    next_delivery_attempt_at: str | None = None
    acknowledged_at: str | None = None
    acknowledged_by: str | None = None
    muted_until: str | None = None
    resolved_at: str | None = None
    created_at: str
    updated_at: str


class ProjectJobBatchReport(BaseModel):
    batch_id: str
    project_id: str
    source: str
    created_at: str
    first_render_at: str | None = None
    last_updated_at: str | None = None
    completed_at: str | None = None
    job_count: int
    planned_output_count: int
    output_count: int
    pending_review_output_count: int
    approved_output_count: int
    rejected_output_count: int
    approval_rate: float
    xai_cost_usd: float
    xai_cost_in_usd_ticks: int
    status_counts: ProjectJobBatchStatusCounts
    review_counts: ProjectJobBatchReviewCounts
    failure_counts: ProjectJobBatchFailureCounts
    alerts: list[ProjectJobBatchAlert] = Field(default_factory=list)


class RenderResult(BaseModel):
    """One render projected into the Mise worker-contract shape.

    Mise consumes a strict ``{"renders": [...]}`` envelope to enforce its spend
    cap and persist provenance. ``source_asset_id`` is sketched as an int in the
    contract, but Aphrodite uses opaque string asset ids; the real id (or null)
    is emitted as-is for Mise to map.
    """

    source_asset_id: str | None = None
    kind: str
    spec: dict[str, Any] | str
    output_path: str | None = None
    cost_usd: float = Field(ge=0)
    model: str | None = None
    latency_ms: int | None = None
    review_status: OutputReviewStatus | None = None
    rights_confirmed: bool = False


class RenderResultEnvelope(BaseModel):
    renders: list[RenderResult] = Field(default_factory=list)


class JobStatusUpdate(BaseModel):
    status: JobStatus
    error: str | None = Field(default=None, max_length=2000)
    failure_category: JobFailureCategory | None = None

    @model_validator(mode="after")
    def failed_jobs_need_error_message(self) -> JobStatusUpdate:
        if self.status == JobStatus.FAILED and not self.error:
            raise ValueError("error is required when status is failed")
        return self


class WorkerClaimRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    worker_id: str = Field(min_length=1, max_length=120)
    claim_ttl_seconds: int = Field(default=300, ge=10, le=3600)


class WorkerClaimRefreshRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    claim_token: str = Field(min_length=1, max_length=120)
    claim_ttl_seconds: int = Field(default=300, ge=10, le=3600)


class WorkerJobClaim(BaseModel):
    job: JobRecord
    claim_token: str
    claim_expires_at: str


class JobOutputCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    claim_token: str = Field(min_length=1, max_length=120)
    variant_id: str = Field(min_length=1, max_length=120)
    storage_path: str = Field(min_length=1, max_length=2048)
    content_type: str = Field(min_length=1, max_length=120)
    bytes: int = Field(ge=0)
    sha256: str = Field(min_length=16, max_length=128)
    width: int = Field(ge=1)
    height: int = Field(ge=1)
    # The renderer reports the ACTUAL spend (cost_usd, never under-reported) plus
    # provenance so Mise can enforce its hard spend cap and persist a cost
    # report. Defaulted so workers that predate the cost contract still validate.
    cost_usd: float = Field(default=0.0, ge=0)
    cost_ticks: int | None = Field(default=None, ge=0)
    model: str | None = Field(default=None, max_length=200)
    latency_ms: int | None = Field(default=None, ge=0)
    # Stable per render attempt. A duplicated delivery of the same attempt is an
    # idempotent no-op so retries never double-charge or revoke approval.
    render_request_id: str | None = Field(default=None, max_length=120)


class OutputRightsConfirmation(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    confirmed_by: str = Field(min_length=1, max_length=200)
    license_note: str | None = Field(default=None, max_length=2000)


class JobFailureRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    claim_token: str = Field(min_length=1, max_length=120)
    error: str = Field(min_length=1, max_length=2000)
    failure_category: JobFailureCategory | None = None


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


def _variant_spec(variant: OutputVariant) -> dict[str, Any]:
    return {
        "variant_id": variant.id,
        "target_id": variant.target_id,
        "label": variant.label,
        "width": variant.width,
        "height": variant.height,
        "aspect_ratio": variant.aspect_ratio,
        "output_format": variant.output_format,
        "background": variant.background,
        "safe_margin_percent": variant.safe_margin_percent,
    }


def render_request_key(*, job: JobRecord, variant: OutputVariant) -> str:
    """Deterministic idempotency key for a single render.

    Stable per ``(source_asset_id, spec, request)`` so any retry, re-claim, or
    re-delivery of the same render — even from a fresh worker process — derives
    the identical key. The API recognizes the repeat and treats it as an
    idempotent no-op rather than a second render/charge. A new request (a new
    job) yields a new key, so intentional regeneration is unaffected.
    """

    material = json.dumps(
        {
            "source_asset_id": job.source_asset_id,
            "source_image_uri": job.product.source_image_uri,
            "spec": _variant_spec(variant),
            "job_id": job.id,
            "variant_id": variant.id,
        },
        sort_keys=True,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def build_render_results(job: JobRecord) -> RenderResultEnvelope:
    """Project a job's persisted outputs into the Mise renders-JSON envelope.

    One render entry per produced output, carrying the real per-render cost so
    Mise can sum it against its hard cap. Read-only: this never mutates state and
    never publishes anything.
    """

    variants_by_id = {variant.id: variant for variant in job.output_plan}
    renders: list[RenderResult] = []
    for output in job.outputs:
        variant = variants_by_id.get(output.variant_id)
        renders.append(
            RenderResult(
                source_asset_id=job.source_asset_id,
                kind=variant.target_id if variant is not None else output.variant_id,
                spec=_variant_spec(variant) if variant is not None else output.variant_id,
                output_path=output.storage_path,
                cost_usd=output.cost_usd,
                model=output.model,
                latency_ms=output.latency_ms,
                review_status=output.review_status,
                rights_confirmed=output.rights_confirmed_at is not None,
            )
        )
    return RenderResultEnvelope(renders=renders)
