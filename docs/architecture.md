# Aphrodite Architecture

Aphrodite owns the product-photography workflow for e-commerce assets.

## Current service boundary

The first slices are an API, asset intake, and planning service. Aphrodite accepts
source product image uploads, stores asset metadata, accepts product image job requests,
expands marketplace-style targets into output variants, and exposes status transitions
for a future renderer or operator.

```text
source product image -> asset intake -> job request -> output plan -> renderer -> QA/export
                           ^             ^              ^
                           |             |              |
                     Aphrodite API  current scope   current scope
```

## Modules

- `aphrodite.api`: FastAPI routes and application factory.
- `aphrodite.assets`: upload validation, metadata extraction, and local asset writes.
- `aphrodite.config`: environment-backed settings.
- `aphrodite.domain`: asset, request, job, status, and output variant models.
- `aphrodite.marketplaces`: starter output preset registry.
- `aphrodite.store`: SQLite repository for durable jobs.

## Near-term integrations

The renderer should consume queued jobs, write generated asset records, and move jobs
through `planning`, `rendering`, `completed`, or `failed`. Generation backends should
stay behind a small interface so the service can support a local ComfyUI path first and
a hosted model path later without changing the API contract.
