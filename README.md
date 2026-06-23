# Aphrodite

AI product photography for e-commerce: packshots, backgrounds, and marketplace-ready
image variants.

Aphrodite starts as a small service for turning a source product image into a durable
generation job. The current foundation does not render images yet; it defines the API,
job persistence, marketplace preset planning, and health checks that the rendering
pipeline will build on.

## What is in place

- FastAPI service with health and readiness endpoints.
- SQLite-backed source asset and product photo job persistence.
- Upload intake for PNG/JPEG product originals with checksum and dimensions.
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

## Next build targets

- Add a renderer worker contract with pluggable local or remote generation backends.
- Add QA/export records for approved variants.
- Add auth and project/client ownership once Aphrodite is wired into the wider stack.
