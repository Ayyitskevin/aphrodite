# Aphrodite Architecture

Aphrodite owns the product-photography workflow for e-commerce assets.

## Current service boundary

The first slices are an API, asset intake, planning service, and renderer worker
contract. Aphrodite accepts source product image uploads, stores asset metadata, accepts
product image job requests, expands marketplace-style targets into output variants, and
lets renderers claim jobs, heartbeat claims, complete outputs, or fail jobs.

```text
source product image -> asset intake -> job request -> output plan -> renderer -> QA/export
                           ^             ^              ^           ^
                           |             |              |           |
                     Aphrodite API  current scope   current scope  worker contract
```

## Modules

- `aphrodite.api`: FastAPI routes, admin routes, and application factory.
- `aphrodite.admin`: operator HTML views and xAI spend ledger parsing.
- `aphrodite.assets`: upload validation, metadata extraction, and local asset writes.
- `aphrodite.config`: environment-backed settings.
- `aphrodite.domain`: asset, request, job, status, worker claim, and output models.
- `aphrodite.marketplaces`: starter output preset registry.
- `aphrodite.renderers`: renderer backend protocol and deterministic local stub backend.
- `aphrodite.xai`: xAI Grok Imagine backend, REST client, prompt builder, and cost guard.
- `aphrodite.storage`: safe local output path handling and file metadata.
- `aphrodite.store`: SQLite repository for durable assets, jobs, claims, and outputs.
- `aphrodite.worker`: HTTP worker client and CLI polling loop.

## Near-term integrations

The `aphrodite-worker` CLI consumes queued jobs through `POST /v1/worker/jobs/claim`,
refreshes claims while rendering, writes generated output records, and moves jobs to
`completed` or `failed`. Generation backends stay behind the `RendererBackend` interface
so the service can support deterministic `local_stub` outputs and the hosted `xai`
backend without changing the API contract. The local stub writes deterministic
placeholder artifacts under `media/outputs/{job_id}/...`. The xAI backend calls Grok
Imagine, stores generated image bytes under the same output layout, and records returned
cost ticks in a local JSONL ledger.

Claims are short-lived and token-scoped. A queued job can be claimed once, and an expired
`rendering` claim can be recovered by another worker. Output completion is accepted only
while the worker holds an active claim token.
