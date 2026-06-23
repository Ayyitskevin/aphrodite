# Aphrodite

AI product photography for e-commerce: packshots, backgrounds, and marketplace-ready
image variants.

Aphrodite starts as a small service for turning a source product image into a durable
generation job. The current foundation does not render images yet; it defines the API,
job persistence, marketplace preset planning, and health checks that the rendering
pipeline will build on.

## What is in place

- FastAPI service with health and readiness endpoints.
- SQLite-backed source asset, product photo job, and generated output persistence.
- Upload intake for PNG/JPEG product originals with checksum and dimensions.
- Worker claim contract for renderers with heartbeats, stale claim recovery, and outputs.
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

Upload a source product image:

```bash
curl -s http://127.0.0.1:8020/v1/assets \
  -F file=@/path/to/product.png
```

Create a generation job:

```bash
curl -s http://127.0.0.1:8020/v1/jobs \
  -H 'Content-Type: application/json' \
  -d '{
    "source_asset_id": "<asset id from /v1/assets>",
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
aphrodite-worker --backend local_stub --media-root media --once
```

Claim and complete a queued job manually as a renderer:

```bash
curl -s http://127.0.0.1:8020/v1/worker/jobs/claim \
  -H 'Content-Type: application/json' \
  -d '{"worker_id":"local-renderer"}'

curl -s http://127.0.0.1:8020/v1/worker/jobs/<job id>/outputs \
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
| `APHRODITE_HOST` | `127.0.0.1` | Host used by the `aphrodite-api` script. |
| `APHRODITE_PORT` | `8020` | Port used by the `aphrodite-api` script. |
| `APHRODITE_RELOAD` | `false` | Enables uvicorn reload for local development. |
| `APHRODITE_WORKER_API_URL` | `http://127.0.0.1:8020` | API base URL used by `aphrodite-worker`. |
| `APHRODITE_WORKER_ID` | host-derived | Worker identity used when claiming jobs. |
| `APHRODITE_WORKER_BACKEND` | `local_stub` | Renderer backend used by the worker CLI. |
| `APHRODITE_WORKER_MEDIA_ROOT` | `media` | Shared media root where worker outputs are written. |
| `APHRODITE_WORKER_POLL_SECONDS` | `5` | Idle polling delay for the worker CLI. |
| `APHRODITE_WORKER_CLAIM_TTL_SECONDS` | `300` | Claim heartbeat/expiry window. |
| `APHRODITE_WORKER_ONCE` | `false` | Process at most one claim and exit. |

## Next build targets

- Add a ComfyUI backend behind the renderer interface.
- Add QA/export records for approved variants.
- Add auth and project/client ownership once Aphrodite is wired into the wider stack.
