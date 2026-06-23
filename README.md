# Aphrodite

AI product photography for e-commerce: packshots, backgrounds, and marketplace-ready
image variants.

Aphrodite starts as a small service for turning a source product image into a durable
generation job. The foundation now covers API intake, client/project ownership, job
persistence, marketplace preset planning, renderer worker contracts, review, and export.

## What is in place

- FastAPI service with health and readiness endpoints.
- SQLite-backed client, project, source asset, product photo job, generated output, review, and export persistence.
- Upload intake for PNG/JPEG product originals with checksum and dimensions.
- Worker claim contract for renderers with heartbeats, stale claim recovery, and outputs.
- Guarded xAI Grok Imagine renderer backend for real generated product outputs.
- Domain models for product inputs, source assets, background intent, output variants, and job status.
- Starter marketplace-style output presets for catalog, social, hero, and transparent
  packshot variants.
- Tests for the domain planner, store, and API.
- GitHub Actions CI for linting and tests.

## Local development

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[dev]"
cp .env.example .env
uvicorn aphrodite.main:app --host 127.0.0.1 --port 8020 --reload
```

Health checks:

```bash
curl http://127.0.0.1:8020/healthz
curl http://127.0.0.1:8020/readiness
```

Admin views:

- `http://127.0.0.1:8020/admin/jobs`
- `http://127.0.0.1:8020/admin/jobs?review=needs_review`
- `http://127.0.0.1:8020/admin/jobs?client_id=<client id>`
- `http://127.0.0.1:8020/admin/jobs?project_id=<project id>`
- `http://127.0.0.1:8020/admin/spend.json`

Completed outputs enter `pending_review`. Operators can approve or reject variants from
the job detail page. Only approved outputs are available through the single-output export
link or the approved-output ZIP export.

Upload a source product image:

```bash
curl -s http://127.0.0.1:8020/v1/assets \
  -H "Authorization: Bearer $APHRODITE_API_TOKEN" \
  -F file=@/path/to/product.png
```

Omit the `Authorization` header in local development when `APHRODITE_API_TOKEN`
is unset.

Create ownership records:

```bash
curl -s http://127.0.0.1:8020/v1/clients \
  -H "Authorization: Bearer $APHRODITE_API_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"name":"Maison Example","external_id":"client-001"}'

curl -s http://127.0.0.1:8020/v1/projects \
  -H "Authorization: Bearer $APHRODITE_API_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"client_id":"<client id>","name":"Spring catalog","external_id":"catalog-001"}'
```

Create a batch of project-owned generation jobs:

```bash
curl -s http://127.0.0.1:8020/v1/projects/<project id>/jobs/batch \
  -H "Authorization: Bearer $APHRODITE_API_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
    "marketplace_targets": ["catalog_square", "transparent_cutout"],
    "priority": 6,
    "items": [
      {
        "product": {
          "name": "Matte ceramic mug",
          "sku": "MUG-001",
          "source_image_uri": "file:///media/mug/source.jpg"
        }
      },
      {
        "product": {
          "name": "Canvas tote",
          "sku": "TOTE-001",
          "source_image_uri": "file:///media/tote/source.jpg"
        },
        "marketplace_targets": ["social_square"]
      }
    ]
  }'
```

Import a CSV or Google Sheet export into a project batch:

```bash
curl -s -OJ http://127.0.0.1:8020/v1/catalog-import/template.csv

curl -s http://127.0.0.1:8020/v1/projects/<project id>/jobs/batch/csv \
  -H "Authorization: Bearer $APHRODITE_API_TOKEN" \
  -F file=@catalog.csv \
  -F marketplace_targets=catalog_square,transparent_cutout \
  -F priority=6
```

Supported CSV columns are `name`, `sku`, `category`, `instructions`,
`source_image_uri`, `source_asset_id`, `marketplace_targets`, `background_style`,
`background_prompt`, `quantity_per_target`, and `priority`. Export a Google Sheet as CSV
with the same headers to use the same route.

Create a single generation job:

```bash
curl -s http://127.0.0.1:8020/v1/jobs \
  -H "Authorization: Bearer $APHRODITE_API_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
    "source_asset_id": "<asset id from /v1/assets>",
    "project_id": "<optional project id>",
    "product": {
      "name": "Matte ceramic mug",
      "sku": "MUG-001"
    },
    "marketplace_targets": ["catalog_square", "transparent_cutout"],
    "background": {
      "style": "studio_shadow",
      "prompt": "soft studio light, clean premium product surface"
    }
  }'
```

Run the local stub renderer worker:

```bash
aphrodite-worker --backend local_stub --media-root media --token "$APHRODITE_WORKER_TOKEN" --once
```

Run one guarded xAI render:

```bash
export APHRODITE_XAI_API_KEY=...
export APHRODITE_XAI_DAILY_BUDGET_USD=0.10
aphrodite-worker --backend xai --media-root media --token "$APHRODITE_WORKER_TOKEN" --once
```

The xAI backend uses uploaded source assets as edit inputs when available, otherwise it
falls back to prompt-only generation. Costs returned by xAI are appended to
`media/.aphrodite-xai-costs.jsonl` by default.

Claim and complete a queued job manually as a renderer:

```bash
curl -s http://127.0.0.1:8020/v1/worker/jobs/claim \
  -H "Authorization: Bearer $APHRODITE_WORKER_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"worker_id":"local-renderer"}'

curl -s http://127.0.0.1:8020/v1/worker/jobs/<job id>/outputs \
  -H "Authorization: Bearer $APHRODITE_WORKER_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
    "claim_token": "<claim token>",
    "variant_id": "catalog_square",
    "storage_path": "outputs/catalog_square.jpg",
    "content_type": "image/jpeg",
    "bytes": 1024,
    "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "width": 2000,
    "height": 2000
  }'
```

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `APHRODITE_ENV` | `development` | Runtime environment label. |
| `APHRODITE_DB_PATH` | `data/aphrodite.db` | SQLite database path. |
| `APHRODITE_MEDIA_ROOT` | `media` | Local root for uploaded product originals. |
| `APHRODITE_MAX_UPLOAD_BYTES` | `15000000` | Maximum upload size for one source image. |
| `APHRODITE_API_TOKEN` | unset | Optional bearer token required for mutating API routes when set. |
| `APHRODITE_WORKER_TOKEN` | unset | Optional bearer token required for worker routes; falls back to `APHRODITE_API_TOKEN` when unset. |
| `APHRODITE_HOST` | `127.0.0.1` | Host used by the `aphrodite-api` script. |
| `APHRODITE_PORT` | `8020` | Port used by the `aphrodite-api` script. |
| `APHRODITE_RELOAD` | `false` | Enables uvicorn reload for local development. |
| `APHRODITE_WORKER_API_URL` | `http://127.0.0.1:8020` | API base URL used by `aphrodite-worker`. |
| `APHRODITE_WORKER_ID` | host-derived | Worker identity used when claiming jobs. |
| `APHRODITE_WORKER_BACKEND` | `local_stub` | Renderer backend used by the worker CLI (`local_stub` or `xai`). |
| `APHRODITE_WORKER_MEDIA_ROOT` | `media` | Shared media root where worker outputs are written. |
| `APHRODITE_XAI_API_KEY` | unset | xAI bearer token for the `xai` renderer; `XAI_API_KEY` also works. |
| `APHRODITE_XAI_MODEL` | `grok-imagine-image-quality` | xAI image model. |
| `APHRODITE_XAI_BASE_URL` | `https://api.x.ai/v1` | xAI API base URL. |
| `APHRODITE_XAI_TIMEOUT_SECONDS` | `60` | xAI request timeout. |
| `APHRODITE_XAI_MAX_RETRIES` | `1` | Retries for transient xAI errors. |
| `APHRODITE_XAI_RESOLUTION` | `1k` | Requested xAI image resolution. |
| `APHRODITE_XAI_ESTIMATED_IMAGE_COST_USD` | `0.10` | Preflight cost estimate used for budget guards. |
| `APHRODITE_XAI_MAX_IMAGE_COST_USD` | `0.25` | Per-image preflight limit. |
| `APHRODITE_XAI_DAILY_BUDGET_USD` | `1.00` | Local daily budget guard before real xAI calls. |
| `APHRODITE_XAI_COST_LEDGER_PATH` | `media/.aphrodite-xai-costs.jsonl` | Local JSONL cost ledger path. |
| `APHRODITE_WORKER_POLL_SECONDS` | `5` | Idle polling delay for the worker CLI. |
| `APHRODITE_WORKER_CLAIM_TTL_SECONDS` | `300` | Claim heartbeat/expiry window. |
| `APHRODITE_WORKER_ONCE` | `false` | Process at most one claim and exit. |

## Next build targets

- Add project-level export bundles and review dashboards.
- Add an admin catalog import screen on top of the CSV batch API.
