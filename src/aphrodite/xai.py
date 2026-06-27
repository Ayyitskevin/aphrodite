"""xAI Grok Imagine renderer backend."""

from __future__ import annotations

import base64
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib import error, request

from PIL import Image, UnidentifiedImageError

from aphrodite.domain import JobRecord, OutputVariant
from aphrodite.renderers import RenderedOutput, RendererError
from aphrodite.storage import output_relative_path, write_output_file

LOG = logging.getLogger(__name__)
TICKS_PER_USD = 10_000_000_000
DEFAULT_MODEL = "grok-imagine-image-quality"
DEFAULT_BASE_URL = "https://api.x.ai/v1"
TRANSIENT_HTTP_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


@dataclass(frozen=True, slots=True)
class XAIImageConfig:
    api_key: str | None
    model: str = DEFAULT_MODEL
    base_url: str = DEFAULT_BASE_URL
    timeout_seconds: float = 60.0
    max_retries: int = 1
    resolution: str = "1k"
    estimated_image_cost_ticks: int = 1_000_000_000
    max_image_cost_ticks: int = 2_500_000_000
    daily_budget_ticks: int = 10_000_000_000
    cost_ledger_path: str | None = None

    @classmethod
    def from_env(cls, *, media_root: str = "media") -> XAIImageConfig:
        return cls(
            api_key=os.getenv("APHRODITE_XAI_API_KEY") or os.getenv("XAI_API_KEY") or None,
            model=os.getenv("APHRODITE_XAI_MODEL", DEFAULT_MODEL),
            base_url=os.getenv("APHRODITE_XAI_BASE_URL", DEFAULT_BASE_URL),
            timeout_seconds=_env_float("APHRODITE_XAI_TIMEOUT_SECONDS", 60.0),
            max_retries=_env_int("APHRODITE_XAI_MAX_RETRIES", 1),
            resolution=os.getenv("APHRODITE_XAI_RESOLUTION", "1k"),
            estimated_image_cost_ticks=_usd_env_to_ticks(
                "APHRODITE_XAI_ESTIMATED_IMAGE_COST_USD",
                0.10,
            ),
            max_image_cost_ticks=_usd_env_to_ticks("APHRODITE_XAI_MAX_IMAGE_COST_USD", 0.25),
            daily_budget_ticks=_usd_env_to_ticks("APHRODITE_XAI_DAILY_BUDGET_USD", 1.00),
            cost_ledger_path=os.getenv("APHRODITE_XAI_COST_LEDGER_PATH")
            or str(Path(media_root) / ".aphrodite-xai-costs.jsonl"),
        )


@dataclass(frozen=True, slots=True)
class XAIImageResponse:
    image: bytes
    mime_type: str | None
    revised_prompt: str | None
    cost_ticks: int


class XAIImageClient:
    def __init__(self, config: XAIImageConfig) -> None:
        if not config.api_key:
            raise RendererError("xAI renderer requires APHRODITE_XAI_API_KEY or XAI_API_KEY")
        self.config = config

    def render_image(
        self,
        *,
        prompt: str,
        aspect_ratio: str,
        source_image: dict[str, str] | None = None,
    ) -> XAIImageResponse:
        body: dict[str, Any] = {
            "model": self.config.model,
            "prompt": prompt,
            "n": 1,
            "response_format": "b64_json",
            "aspect_ratio": aspect_ratio,
            "resolution": self.config.resolution,
        }
        endpoint = "/images/generations"
        if source_image is not None:
            endpoint = "/images/edits"
            body["image"] = source_image

        response = self._post_json(endpoint, body)
        return self._parse_image_response(response)

    def _post_json(self, endpoint: str, body: dict[str, Any]) -> dict[str, Any]:
        data = json.dumps(body).encode("utf-8")
        url = f"{self.config.base_url.rstrip('/')}{endpoint}"
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }

        for attempt in range(self.config.max_retries + 1):
            req = request.Request(url, data=data, headers=headers, method="POST")
            try:
                with request.urlopen(req, timeout=self.config.timeout_seconds) as response:
                    return json.loads(response.read().decode("utf-8"))
            except error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")
                if exc.code not in TRANSIENT_HTTP_STATUS or attempt >= self.config.max_retries:
                    raise RendererError(f"xAI image request failed with HTTP {exc.code}") from exc
                LOG.info(
                    "retrying transient xAI image error",
                    extra={"status_code": exc.code, "detail": detail[:240]},
                )
            except OSError as exc:
                if attempt >= self.config.max_retries:
                    raise RendererError(f"xAI image request failed: {exc}") from exc
            time.sleep(min(2**attempt, 8))

        raise RendererError("xAI image request failed after retries")

    def _parse_image_response(self, payload: dict[str, Any]) -> XAIImageResponse:
        data = payload.get("data")
        if not isinstance(data, list) or not data:
            raise RendererError("xAI image response did not include generated data")
        first = data[0]
        if not isinstance(first, dict):
            raise RendererError("xAI image response item is invalid")

        image_bytes = _image_bytes_from_response_item(first, self.config.timeout_seconds)
        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        cost_ticks = int(usage.get("cost_in_usd_ticks") or 0)
        revised_prompt = first.get("revised_prompt")
        return XAIImageResponse(
            image=image_bytes,
            mime_type=first.get("mime_type"),
            revised_prompt=revised_prompt if isinstance(revised_prompt, str) else None,
            cost_ticks=cost_ticks,
        )


class XAIImageRendererBackend:
    name = "xai"

    def __init__(
        self,
        *,
        media_root: str = "media",
        config: XAIImageConfig | None = None,
        client: XAIImageClient | None = None,
    ) -> None:
        self.media_root = media_root
        self.config = config or XAIImageConfig.from_env(media_root=media_root)
        self.client = client or XAIImageClient(self.config)
        self.cost_guard = XAIImageCostGuard(self.config)

    @classmethod
    def from_env(cls, *, media_root: str = "media") -> XAIImageRendererBackend:
        return cls(media_root=media_root, config=XAIImageConfig.from_env(media_root=media_root))

    def render(self, *, job: JobRecord, variant: OutputVariant) -> RenderedOutput:
        self.cost_guard.assert_can_start(job=job, variant=variant)
        source_image = self._source_image_payload(job)
        started = time.monotonic()
        response = self.client.render_image(
            prompt=_prompt_for(job=job, variant=variant, has_source=source_image is not None),
            aspect_ratio=_aspect_ratio_for(variant),
            source_image=source_image,
        )
        latency_ms = max(0, int((time.monotonic() - started) * 1000))
        self.cost_guard.record_spend(job=job, variant=variant, cost_ticks=response.cost_ticks)

        content_type, extension, width, height = _decode_output_image(
            response.image,
            response.mime_type,
        )
        storage_path = output_relative_path(
            job_id=job.id,
            variant_id=variant.id,
            extension=extension,
        )
        stored = write_output_file(
            media_root=self.media_root,
            relative_path=storage_path,
            content=response.image,
        )
        return RenderedOutput(
            variant_id=variant.id,
            storage_path=stored.relative_path,
            content_type=content_type,
            bytes=stored.bytes,
            sha256=stored.sha256,
            width=width,
            height=height,
            # Forward the ACTUAL spend reported by xAI (ticks -> USD) plus the
            # model and call latency so the API and Mise see real cost, not just
            # the local cost ledger written by the cost guard.
            cost_usd=response.cost_ticks / TICKS_PER_USD,
            cost_ticks=response.cost_ticks,
            model=self.config.model,
            latency_ms=latency_ms,
        )

    def _source_image_payload(self, job: JobRecord) -> dict[str, str] | None:
        if job.source_asset is not None:
            source_path = Path(self.media_root) / job.source_asset.storage_path
            if not source_path.exists():
                raise RendererError(f"source asset file not found: {job.source_asset.storage_path}")
            encoded = base64.b64encode(source_path.read_bytes()).decode("ascii")
            return {
                "type": "image_url",
                "url": f"data:{job.source_asset.content_type};base64,{encoded}",
            }

        source_uri = job.product.source_image_uri or ""
        if source_uri.startswith(("http://", "https://")):
            return {"type": "image_url", "url": source_uri}
        return None


class XAIImageCostGuard:
    def __init__(self, config: XAIImageConfig) -> None:
        self.config = config

    def assert_can_start(self, *, job: JobRecord, variant: OutputVariant) -> None:
        if self.config.estimated_image_cost_ticks > self.config.max_image_cost_ticks:
            raise RendererError("xAI estimated image cost exceeds per-image limit")

        today_spend = self._spent_today_ticks()
        if today_spend + self.config.estimated_image_cost_ticks > self.config.daily_budget_ticks:
            raise RendererError("xAI daily budget would be exceeded by this render")

    def record_spend(self, *, job: JobRecord, variant: OutputVariant, cost_ticks: int) -> None:
        if not self.config.cost_ledger_path:
            return
        path = Path(self.config.cost_ledger_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "date": date.today().isoformat(),
            "recorded_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "job_id": job.id,
            "variant_id": variant.id,
            "model": self.config.model,
            "cost_in_usd_ticks": cost_ticks,
            "cost_usd": cost_ticks / TICKS_PER_USD,
        }
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, sort_keys=True) + "\n")

        if cost_ticks > self.config.max_image_cost_ticks:
            LOG.warning(
                "xAI image cost exceeded configured per-image limit after completion",
                extra={
                    "job_id": job.id,
                    "variant_id": variant.id,
                    "cost_ticks": cost_ticks,
                    "limit_ticks": self.config.max_image_cost_ticks,
                },
            )

    def _spent_today_ticks(self) -> int:
        if not self.config.cost_ledger_path:
            return 0
        path = Path(self.config.cost_ledger_path)
        if not path.exists():
            return 0

        today = date.today().isoformat()
        total = 0
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("date") == today:
                    total += int(entry.get("cost_in_usd_ticks") or 0)
        return total


def _image_bytes_from_response_item(item: dict[str, Any], timeout: float) -> bytes:
    b64_json = item.get("b64_json")
    if isinstance(b64_json, str) and b64_json:
        return base64.b64decode(b64_json)

    url = item.get("url")
    if isinstance(url, str) and url:
        with request.urlopen(url, timeout=timeout) as response:
            return response.read()

    raise RendererError("xAI image response did not include b64_json or url")


def _prompt_for(*, job: JobRecord, variant: OutputVariant, has_source: bool) -> str:
    lines = [
        "Create a marketplace-ready ecommerce product image.",
        f"Product: {job.product.name}.",
        f"Target: {variant.label} at {variant.aspect_ratio}.",
        f"Background: {variant.background}.",
        f"Safe margin: {variant.safe_margin_percent} percent.",
        "Keep the product centered, sharp, commercially usable, and free of text overlays.",
    ]
    if has_source:
        lines.append("Use the supplied product image as the source subject.")
    if job.product.sku:
        lines.append(f"SKU: {job.product.sku}.")
    if job.product.category:
        lines.append(f"Category: {job.product.category}.")
    if job.product.instructions:
        lines.append(f"Product instructions: {job.product.instructions}.")
    if variant.prompt:
        lines.append(f"Creative direction: {variant.prompt}.")
    return " ".join(lines)


def _aspect_ratio_for(variant: OutputVariant) -> str:
    supported = {
        "1:1",
        "16:9",
        "9:16",
        "4:3",
        "3:4",
        "3:2",
        "2:3",
        "2:1",
        "1:2",
        "19.5:9",
        "9:19.5",
        "20:9",
        "9:20",
    }
    return variant.aspect_ratio if variant.aspect_ratio in supported else "auto"


def _decode_output_image(
    content: bytes,
    declared_mime_type: str | None,
) -> tuple[str, str, int, int]:
    try:
        with Image.open(BytesIO(content)) as image:
            image.verify()
        with Image.open(BytesIO(content)) as image:
            image_format = (image.format or "").upper()
            width, height = image.size
    except (SyntaxError, UnidentifiedImageError, OSError, ValueError) as exc:
        raise RendererError("xAI returned invalid image bytes") from exc

    detected = _content_type_and_extension(image_format)
    if detected is None:
        raise RendererError(f"xAI returned unsupported image format: {image_format or 'unknown'}")

    content_type, extension = detected
    if declared_mime_type in {"image/jpeg", "image/png", "image/webp"}:
        content_type = declared_mime_type
        extension = _extension_for_content_type(content_type)
    return content_type, extension, width, height


def _content_type_and_extension(image_format: str) -> tuple[str, str] | None:
    if image_format == "JPEG":
        return "image/jpeg", "jpg"
    if image_format == "PNG":
        return "image/png", "png"
    if image_format == "WEBP":
        return "image/webp", "webp"
    return None


def _extension_for_content_type(content_type: str) -> str:
    if content_type == "image/jpeg":
        return "jpg"
    if content_type == "image/png":
        return "png"
    if content_type == "image/webp":
        return "webp"
    return "bin"


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a float") from exc


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _usd_env_to_ticks(name: str, default: float) -> int:
    return int(_env_float(name, default) * TICKS_PER_USD)
