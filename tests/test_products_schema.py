"""Validate Aphrodite's renders output against the canonical products JSON Schema.

The schema (schemas/products_render.schema.json) is the artifact Mise validates
against. These tests prove the schema is well-formed, that real projection output
conforms, and that the schema actually rejects malformed renders.
"""

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from aphrodite.domain import (
    JobCreate,
    JobOutputCreate,
    ProductInput,
    RenderResult,
    RenderResultEnvelope,
    build_render_results,
)
from aphrodite.store import JobStore

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schemas" / "products_render.schema.json"


def _schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text())


def _validator() -> Draft202012Validator:
    return Draft202012Validator(_schema())


def _completed_job(tmp_path: Path, *, quantity: int = 1):
    store = JobStore(str(tmp_path / "db.sqlite"))
    store.initialize()
    created = store.create_job(
        JobCreate(
            product=ProductInput(name="Mug", source_image_uri="file:///mug.jpg"),
            marketplace_targets=["catalog_square"],
            quantity_per_target=quantity,
        )
    )
    claim = store.claim_next_job(worker_id="renderer")
    assert claim is not None
    for variant in claim.job.output_plan:
        store.complete_job_output(
            job_id=created.id,
            output=JobOutputCreate(
                claim_token=claim.claim_token,
                variant_id=variant.id,
                storage_path=f"outputs/{variant.id}.jpg",
                content_type="image/jpeg",
                bytes=10,
                sha256="a" * 64,
                width=2000,
                height=2000,
                cost_usd=0.05,
                model="grok-imagine-image-quality",
            ),
        )
    job = store.get_job(created.id)
    assert job is not None
    return job


def test_schema_is_a_valid_json_schema() -> None:
    # Raises if the schema document itself is malformed.
    Draft202012Validator.check_schema(_schema())


def test_real_projection_output_conforms(tmp_path: Path) -> None:
    payload = build_render_results(_completed_job(tmp_path, quantity=2)).model_dump()
    _validator().validate(payload)  # raises ValidationError on non-conformance
    assert len(payload["renders"]) == 2


def test_edge_fixtures_conform() -> None:
    validator = _validator()

    # Null output_path + string spec + zero cost (e.g. a free/stub render).
    null_output = RenderResultEnvelope(
        renders=[
            RenderResult(
                source_asset_id=None,
                kind="catalog_square",
                spec="catalog_square",
                output_path=None,
                cost_usd=0.0,
            )
        ]
    )
    validator.validate(null_output.model_dump())

    # An empty envelope (e.g. a job whose only render failed) is valid and free.
    validator.validate(RenderResultEnvelope().model_dump())


def test_schema_rejects_malformed_renders() -> None:
    validator = _validator()

    # Negative cost is rejected (cost_usd must be a number >= 0).
    with pytest.raises(ValidationError):
        validator.validate(
            {"renders": [{"source_asset_id": None, "kind": "k", "spec": {}, "output_path": None,
                          "cost_usd": -0.01}]}
        )

    # A missing required key is rejected.
    with pytest.raises(ValidationError):
        validator.validate({"renders": [{"kind": "k", "spec": {}, "output_path": None}]})

    # An unknown property is rejected (the contract is closed).
    with pytest.raises(ValidationError):
        validator.validate(
            {"renders": [{"source_asset_id": None, "kind": "k", "spec": {}, "output_path": None,
                          "cost_usd": 0.0, "published_to_client": True}]}
        )
