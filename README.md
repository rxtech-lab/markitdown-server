# markitdown-server

Converts documents at a URL to markdown, using a producer/consumer queue so
large PDFs can be split across worker pods. The original blocking API remains
available, with a separate endpoint for asynchronous callers.

## How it works

```
POST /convert or /async/convert
  │ download → sha256(bytes) → doc_key = file_hash:config_fingerprint
  │
  ├─ document already converted?  ──►  return it. No work queued.
  │
  └─ upload source to S3, plan page ranges, insert job + N chunk rows
                              │
              ┌───────────────┼───────────────┐
           worker A        worker B        worker C
           claim a chunk (atomic UPDATE), extract its page range,
           convert it, write the markdown to S3
                              │
                  last chunk to finish assembles the document,
                  paginates it, and marks the job done
```

Two processes share one image: the **API** (producer) and the **worker**
(consumer, `python -m worker`). Scale by adding worker pods — they coordinate
only through the database, so there is no leader and nothing to reconfigure.

### The queue is a table

`job_chunks` *is* the queue. It already had to track chunk state for progress
and retries, so making it the queue too means one store to keep consistent
rather than two that can disagree. Claiming is a single atomic statement, and
because SQLite serialises writers, two workers cannot claim the same row.

Everything a dedicated queue would provide falls out of three columns:

| Column | Provides |
|---|---|
| `attempts` | retry limit; incremented at *claim* time so an OOM-killed worker still burns an attempt |
| `available_at` | exponential backoff |
| `lease_expires_at` | crash recovery — a lease that stops being renewed is a worker that died |

Workers heartbeat to extend their lease while converting, so the lease means
"the owner is gone" rather than "this is taking a while" — decoupled from how
long a chunk legitimately takes.

### Caching

Storage keys are content-addressed: `sha256(file bytes)` plus a fingerprint of
everything that changes the output (markitdown version, LLM flag, model, page
size, chunk size). Re-submitting a file returns instantly with no conversion
and no LLM spend, and upgrading markitdown invalidates the cache automatically
rather than serving stale markdown forever.

## API

| Endpoint | Purpose |
|---|---|
| `POST /convert` | Convert a URL synchronously. Returns the original `{id, content, pagination}` payload after all queued chunks finish. |
| `POST /async/convert` | Submit a URL asynchronously. Returns `202 {job_id, doc_key}`, or `200` with the first page on a cache hit. |
| `GET /convert/jobs/{job_id}` | Job status and chunk progress. |
| `GET /convert/{doc_key}/pages/{page}` | A page of the converted document. |
| `GET /` | Liveness. Checks nothing external. |
| `GET /readyz` | Readiness. Checks the database and object store. |

All except `/` and `/readyz` require an `x-api-key` header matching
`ADMIN_API_KEY`.

## Configuration

See `config.py` — every environment variable is defined there with its default.
The ones that matter most:

| Variable | Default | Notes |
|---|---|---|
| `TURSO_DATABASE_URL` | `file:markitdown.db` | A path uses stdlib sqlite3; `libsql://` or `http://` uses a libSQL server |
| `TURSO_AUTH_TOKEN` | — | Set for hosted Turso |
| `S3_ENDPOINT` / `S3_BUCKET` | — / `markitdown` | Any S3-compatible store |
| `CONVERT_USE_LLM` | `false` | LLM-assisted conversion (image descriptions); slow and costs tokens per page |
| `PDF_PAGES_PER_CHUNK` | `20` | Smaller chunks lower peak memory per worker |
| `MAX_ATTEMPTS` | `3` | Per chunk; exhausting it fails the whole job |
| `LEASE_TTL` / `HEARTBEAT_INTERVAL` | `120` / `30` | Lease must comfortably exceed the heartbeat |

## Development

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/pip install pytest reportlab httpx "moto[s3]"
.venv/bin/python -m pytest
```

Tests run against stdlib sqlite3 and an in-process moto S3 — no service
containers, no network. `tests/test_integration.py` is the slow one: it does a
real PDF over real HTTP through real markitdown conversion.

### Running locally

`docker-compose.yml` brings up the two dependencies — libSQL (shared database)
and minio (object storage). There is no broker: the queue is a table. The API
and workers run on the host so you keep reload and a debugger.

```bash
make venv     # once
make up       # libsql + minio, bucket created with the 7-day expiry rule
make api      # producer, in another shell
make worker   # consumer — run this several times to add workers
make smoke    # submit a real PDF, poll it, read every page back
```

`make smoke` is the end-to-end check: it converts a real Asimov PDF, walks all
pages back out, and asserts a resubmission hits the content-addressed cache.
Adding worker shells is how you watch chunks distribute — the Foundation
Trilogy splits into 25 chunks and finishes in ~12s across three workers.

Ports default to 8080 (libsql) and 9100/9101 (minio) to avoid the usual
collisions; override with `LIBSQL_PORT` / `MINIO_PORT` in `.env`. `make up`
reads `.env`, and defaults everything it does not find, so `.env` only needs
your real secrets. `make reset` wipes the volumes.

## Deployment

`k8s/` is a release kustomize stack containing the API, worker, HPAs and
ingress. Their RabbitMQ, Turso and S3 connection settings come from
`markitdown-server-secret`; release deployment does not provision those shared
services. The Kubernetes benchmark explicitly deploys the bundled RabbitMQ,
libSQL and MinIO manifests only inside its disposable CI minikube cluster.

`scripts/k8s-benchmark.sh` converts four real Asimov PDFs against a cluster and
enforces per-book time and memory budgets; it is the performance regression
guard and runs in CI on every push.
