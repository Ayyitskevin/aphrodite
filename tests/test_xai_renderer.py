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
    entry = json.loads(ledger.read_text().strip())
    assert entry["job_id"] == "job-123"
    assert entry["variant_id"] == "catalog_square"
    assert entry["cost_in_usd_ticks"] == 200_000_000


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


def test_xai_client_requires_api_key() -> None:
    with pytest.raises(RendererError, match="requires"):
        XAIImageClient(XAIImageConfig(api_key=None))
