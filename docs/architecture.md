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

- `aphrodite.api`: FastAPI routes and application factory.
- `aphrodite.assets`: upload validation, metadata extraction, and local asset writes.
- `aphrodite.config`: environment-backed settings.
- `aphrodite.domain`: asset, request, job, status, worker claim, and output models.
- `aphrodite.marketplaces`: starter output preset registry.
- `aphrodite.renderers`: renderer backend protocol and deterministic local stub backend.
- `aphrodite.store`: SQLite repository for durable assets, jobs, claims, and outputs.
- `aphrodite.worker`: HTTP worker client and CLI polling loop.

## Near-term integrations

The `aphrodite-worker` CLI consumes queued jobs through `POST /v1/worker/jobs/claim`,
refreshes claims while rendering, writes generated output records, and moves jobs to
`completed` or `failed`. Generation backends stay behind the `RendererBackend` interface
so the service can support the deterministic `local_stub` backend now, a local ComfyUI
path next, and a hosted model path later without changing the API contract.

Claims are short-lived and token-scoped. A queued job can be claimed once, and an expired
`rendering` claim can be recovered by another worker. Output completion is accepted only
while the worker holds an active claim token.
