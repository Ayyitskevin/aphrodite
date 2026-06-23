# Aphrodite Architecture

Aphrodite owns the product-photography workflow for e-commerce assets.

## Current service boundary

The first slices are an API, asset intake, planning service, and renderer worker
contract. Aphrodite accepts source product image uploads, stores client and project
ownership records, accepts product image job requests, expands marketplace-style targets
into output variants, lets renderers claim jobs, heartbeat claims, complete outputs, or
fail jobs, and gives operators an approval gate before exports.

```text
client/project -> source product image -> asset intake -> job request -> output plan -> renderer -> QA/export
      ^                                ^             ^              ^           ^
      |                                |             |              |           |
Aphrodite API                    Aphrodite API  current scope   current scope  worker contract
```

## Modules

- `aphrodite.api`: FastAPI routes, admin routes, and application factory.
- `aphrodite.admin`: operator HTML views and xAI spend ledger parsing.
- `aphrodite.assets`: upload validation, metadata extraction, and local asset writes.
- `aphrodite.config`: environment-backed settings.
- `aphrodite.domain`: client, project, asset, request, job, status, worker claim, and output models.
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

Jobs can be linked to a project, and projects belong to clients. The API and admin
job index can filter by either `project_id` or `client_id`. Batch creation expands a
project catalog request into normal queued jobs in one transaction, so renderers keep the
same claim and output contract. CSV imports are parsed into the same batch request shape,
which keeps spreadsheet intake out of worker and renderer code.

Completed outputs start in `pending_review`. Admin review actions can approve or reject
each variant with an optional note, and approved media can be downloaded individually or
as a ZIP. Replacing a rendered output resets its review state so exports cannot silently
serve unapproved regenerated media.
