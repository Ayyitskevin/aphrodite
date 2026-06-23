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
- `aphrodite.store`: SQLite repository for durable assets, jobs, claims, and outputs.

## Near-term integrations

The renderer should consume queued jobs through `POST /v1/worker/jobs/claim`, refresh
claims while rendering, write generated output records, and move jobs to `completed` or
`failed`. Generation backends should stay behind a small interface so the service can
support a local ComfyUI path first and a hosted model path later without changing the
API contract.

Claims are short-lived and token-scoped. A queued job can be claimed once, and an expired
`rendering` claim can be recovered by another worker. Output completion is accepted only
while the worker holds an active claim token.
