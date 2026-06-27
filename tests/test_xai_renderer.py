import base64
import json
from datetime import date
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from aphrodite.domain import AssetRecord, JobRecord, JobStatus, OutputVariant, ProductInput
from aphrodite.renderers import RendererError, get_renderer_backend
from aphrodite.xai import (
    XAIImageClient,
    XAIImageConfig,
    XAIImageCostGuard,
    XAIImageRendererBackend,
    XAIImageResponse,
)


def image_bytes(format_name: str = "JPEG") -> bytes:
    with BytesIO() as buffer:
        Image.new("RGB", (4, 3), (255, 255, 255)).save(buffer, format=format_name)
        return buffer.getvalue()


def source_asset(tmp_path: Path) -> AssetRecord:
    media_root = tmp_path / "media"
    path = media_root / "originals" / "aa" / "asset-1.png"
    path.parent.mkdir(parents=True)
    path.write_bytes(image_bytes("PNG"))
    return AssetRecord(
        id="asset-1",
        original_filename="mug.png",
        content_type="image/png",
        storage_path="originals/aa/asset-1.png",
        bytes=path.stat().st_size,
        sha256="a" * 64,
        width=4,
        height=3,
        created_at="2026-06-23T00:00:00Z",
    )


def job(tmp_path: Path, *, with_asset: bool = True) -> JobRecord:
    asset = source_asset(tmp_path) if with_asset else None
    return JobRecord(
        id="job-123",
        status=JobStatus.RENDERING,
        product=ProductInput(
            name="Matte mug",
            sku="MUG-001",
            category="Drinkware",
            instructions="Keep the handle visible.",
            source_image_uri=None if with_asset else "https://example.test/source.png",
        ),
        source_asset_id=asset.id if asset is not None else None,
        source_asset=asset,
        marketplace_targets=["catalog_square"],
        output_plan=[],
        priority=5,
        created_at="2026-06-23T00:00:00Z",
        updated_at="2026-06-23T00:00:00Z",
    )


def variant() -> OutputVariant:
    return OutputVariant(
        id="catalog_square",
        target_id="catalog_square",
        label="Catalog square",
        width=2000,
        height=2000,
        aspect_ratio="1:1",
        output_format="jpg",
        background="clean_white",
        prompt="soft studio shadow",
        safe_margin_percent=8,
    )


class FakeXAIClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def render_image(self, *, prompt: str, aspect_ratio: str, source_image: dict | None):
        self.calls.append(
            {
                "prompt": prompt,
                "aspect_ratio": aspect_ratio,
                "source_image": source_image,
            }
        )
        return XAIImageResponse(
            image=image_bytes(),
            mime_type="image/jpeg",
            revised_prompt="",
            cost_ticks=200_000_000,
        )


def config(tmp_path: Path, **overrides) -> XAIImageConfig:
    values = {
        "api_key": "secret",
        "cost_ledger_path": str(tmp_path / "media" / ".xai-costs.jsonl"),
    }
    values.update(overrides)
    return XAIImageConfig(**values)


def test_get_renderer_backend_returns_xai_backend(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("APHRODITE_XAI_API_KEY", "secret")
    monkeypatch.setenv("APHRODITE_XAI_COST_LEDGER_PATH", str(tmp_path / "costs.jsonl"))

    backend = get_renderer_backend("xai", media_root=str(tmp_path / "media"))

    assert backend.name == "xai"


def test_xai_renderer_uses_source_asset_and_writes_output(tmp_path: Path) -> None:
    fake_client = FakeXAIClient()
    backend = XAIImageRendererBackend(
        media_root=str(tmp_path / "media"),
        config=config(tmp_path),
        client=fake_client,
    )

    rendered = backend.render(job=job(tmp_path), variant=variant())

    assert rendered.storage_path == "outputs/job-123/catalog_square.jpg"
    assert rendered.content_type == "image/jpeg"
    assert rendered.width == 4
    assert rendered.height == 3
    assert (tmp_path / "media" / rendered.storage_path).exists()

    call = fake_client.calls[0]
    assert "Matte mug" in call["prompt"]
    assert call["aspect_ratio"] == "1:1"
    assert call["source_image"]["type"] == "image_url"
    assert call["source_image"]["url"].startswith("data:image/png;base64,")

    ledger = tmp_path / "media" / ".xai-costs.jsonl"
    entries = [json.loads(line) for line in ledger.read_text().splitlines()]
    assert {entry["job_id"] for entry in entries} == {"job-123"}
    assert {entry["variant_id"] for entry in entries} == {"catalog_square"}
    # A reservation plus its settlement reconcile to the real billed cost.
    assert sum(entry["cost_in_usd_ticks"] for entry in entries) == 200_000_000


def test_xai_renderer_reports_real_cost_and_provenance(tmp_path: Path) -> None:
    backend = XAIImageRendererBackend(
        media_root=str(tmp_path / "media"),
        config=config(tmp_path),
        client=FakeXAIClient(),
    )

    rendered = backend.render(job=job(tmp_path), variant=variant())

    # FakeXAIClient bills 200_000_000 ticks; TICKS_PER_USD = 10_000_000_000.
    assert rendered.cost_ticks == 200_000_000
    assert rendered.cost_usd == pytest.approx(0.02)
    assert rendered.model == "grok-imagine-image-quality"
    assert rendered.latency_ms is not None and rendered.latency_ms >= 0

    payload = rendered.as_worker_payload(claim_token="token")
    assert payload["cost_usd"] == pytest.approx(0.02)
    assert payload["cost_ticks"] == 200_000_000
    assert payload["model"] == "grok-imagine-image-quality"


def test_xai_renderer_uses_public_source_url_without_asset(tmp_path: Path) -> None:
    fake_client = FakeXAIClient()
    backend = XAIImageRendererBackend(
        media_root=str(tmp_path / "media"),
        config=config(tmp_path),
        client=fake_client,
    )

    backend.render(job=job(tmp_path, with_asset=False), variant=variant())

    assert fake_client.calls[0]["source_image"] == {
        "type": "image_url",
        "url": "https://example.test/source.png",
    }


def test_xai_renderer_blocks_when_daily_budget_would_be_exceeded(tmp_path: Path) -> None:
    ledger = tmp_path / "costs.jsonl"
    ledger.write_text(
        json.dumps({"date": date.today().isoformat(), "cost_in_usd_ticks": 9_800_000_000})
        + "\n"
    )
    fake_client = FakeXAIClient()
    backend = XAIImageRendererBackend(
        media_root=str(tmp_path / "media"),
        config=config(
            tmp_path,
            cost_ledger_path=str(ledger),
            daily_budget_ticks=10_000_000_000,
            estimated_image_cost_ticks=500_000_000,
        ),
        client=fake_client,
    )

    with pytest.raises(RendererError, match="daily budget"):
        backend.render(job=job(tmp_path), variant=variant())

    assert fake_client.calls == []


def test_xai_client_posts_base64_generation_request(monkeypatch) -> None:
    captured: dict[str, object] = {}
    payload = {
        "data": [
            {
                "b64_json": base64.b64encode(image_bytes()).decode("ascii"),
                "mime_type": "image/jpeg",
                "revised_prompt": "",
            }
        ],
        "usage": {"cost_in_usd_ticks": 200_000_000},
    }

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self) -> bytes:
            return json.dumps(payload).encode("utf-8")

    def fake_urlopen(req, timeout: float):
        captured["url"] = req.full_url
        captured["authorization"] = req.get_header("Authorization")
        captured["timeout"] = timeout
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return Response()

    monkeypatch.setattr("aphrodite.xai.request.urlopen", fake_urlopen)

    client = XAIImageClient(
        XAIImageConfig(
            api_key="secret",
            timeout_seconds=12,
            model="grok-imagine-image-quality",
        )
    )
    response = client.render_image(prompt="A mug", aspect_ratio="1:1")

    assert captured["url"] == "https://api.x.ai/v1/images/generations"
    assert captured["authorization"] == "Bearer secret"
    assert captured["timeout"] == 12
    assert captured["body"] == {
        "model": "grok-imagine-image-quality",
        "prompt": "A mug",
        "n": 1,
        "response_format": "b64_json",
        "aspect_ratio": "1:1",
        "resolution": "1k",
    }
    assert response.cost_ticks == 200_000_000
    assert response.image.startswith(b"\xff\xd8")


def _ledger_total_ticks(ledger: Path) -> int:
    if not ledger.exists():
        return 0
    return sum(json.loads(line)["cost_in_usd_ticks"] for line in ledger.read_text().splitlines())


def test_xai_renderer_records_billed_spend_when_decode_fails(tmp_path: Path) -> None:
    class BadDecodeClient:
        def render_image(self, *, prompt, aspect_ratio, source_image):
            # xAI bills, then returns bytes that are not a decodable image.
            return XAIImageResponse(
                image=b"not-an-image",
                mime_type="image/jpeg",
                revised_prompt="",
                cost_ticks=200_000_000,
            )

    ledger = tmp_path / "costs.jsonl"
    backend = XAIImageRendererBackend(
        media_root=str(tmp_path / "media"),
        config=config(tmp_path, cost_ledger_path=str(ledger)),
        client=BadDecodeClient(),
    )

    with pytest.raises(RendererError):
        backend.render(job=job(tmp_path), variant=variant())

    # The paid-but-failed generation must still be recorded so spend is visible.
    assert _ledger_total_ticks(ledger) == 200_000_000


def test_xai_renderer_releases_reservation_when_call_fails(tmp_path: Path) -> None:
    class FailingClient:
        def render_image(self, *, prompt, aspect_ratio, source_image):
            raise RendererError("xAI image request failed")

    ledger = tmp_path / "costs.jsonl"
    backend = XAIImageRendererBackend(
        media_root=str(tmp_path / "media"),
        config=config(tmp_path, cost_ledger_path=str(ledger)),
        client=FailingClient(),
    )

    with pytest.raises(RendererError):
        backend.render(job=job(tmp_path), variant=variant())

    # The call never billed, so the reservation is reconciled back to zero.
    assert _ledger_total_ticks(ledger) == 0


def test_unsettled_reservation_blocks_a_concurrent_over_budget_render(tmp_path: Path) -> None:
    ledger = tmp_path / "costs.jsonl"
    guard = XAIImageCostGuard(
        config(
            tmp_path,
            cost_ledger_path=str(ledger),
            daily_budget_ticks=1_500_000_000,
            estimated_image_cost_ticks=1_000_000_000,
            max_image_cost_ticks=1_000_000_000,
        )
    )

    # First render reserves its estimate but has not settled any actual spend.
    guard.assert_can_start(job=job(tmp_path, with_asset=False), variant=variant())

    # A concurrent render must see that reservation and be refused, closing the
    # check-then-act race the old guard left open (it only summed settled spend).
    with pytest.raises(RendererError, match="daily budget"):
        guard.assert_can_start(job=job(tmp_path, with_asset=False), variant=variant())


def test_xai_client_requires_api_key() -> None:
    with pytest.raises(RendererError, match="requires"):
        XAIImageClient(XAIImageConfig(api_key=None))
