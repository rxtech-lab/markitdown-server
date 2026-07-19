"""
Every environment-driven setting in one place.

Two entrypoints (the API pod and the worker pod) run off the same image and
read overlapping subsets of this, so scattering ``os.environ`` reads across
modules made it impossible to see what a given pod actually needs configured.
"""
import os

# Load .env before anything reads the environment, so `uvicorn api.index:app`
# and `python -m worker` work on their own rather than only through `make`.
# Real environment variables always win, so a container's env is not shadowed
# by a stray .env baked into an image.
try:
    from dotenv import load_dotenv

    load_dotenv(override=False)
except ImportError:  # pragma: no cover - python-dotenv is a declared dependency
    pass


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name, str(default)))


def _float(name: str, default: float) -> float:
    return float(os.environ.get(name, str(default)))


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


# ---- Auth ----
ADMIN_API_KEY = os.environ.get("ADMIN_API_KEY")

# ---- Database (libSQL / Turso) ----
# A local file path works as-is; point at libsql://<db>.turso.io with a token
# for the hosted service. Tests use ":memory:".
DATABASE_URL = os.environ.get("TURSO_DATABASE_URL", "file:markitdown.db")
DATABASE_AUTH_TOKEN = os.environ.get("TURSO_AUTH_TOKEN")

# ---- Object storage ----
S3_ENDPOINT = os.environ.get("S3_ENDPOINT") or None
S3_BUCKET = os.environ.get("S3_BUCKET", "markitdown")
S3_REGION = os.environ.get("S3_REGION", "auto")
S3_ACCESS_KEY_ID = os.environ.get("S3_ACCESS_KEY_ID")
S3_SECRET_ACCESS_KEY = os.environ.get("S3_SECRET_ACCESS_KEY")
# Informational only: expiry is enforced by an S3 lifecycle rule on the bucket,
# not by us. Stored on the document row so callers can see when it will vanish.
DOC_TTL_DAYS = _int("DOC_TTL_DAYS", 7)

# ---- Conversion ----
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "google/gemini-3.1-flash-lite-preview")
OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "https://ai-gateway.vercel.sh/v1")
# Whether to run markitdown's LLM-assisted conversion (image descriptions) by
# default. Off by default: it is dramatically slower and costs tokens per page,
# which does not pay for itself on book-sized PDFs.
CONVERT_USE_LLM = _bool("CONVERT_USE_LLM", False)

# ---- Chunking ----
PAGES_PER_CHUNK = _int("PDF_PAGES_PER_CHUNK", 20)
# Below this page count the per-chunk parser startup cost outweighs any
# parallelism, so the document is converted as a single chunk.
MIN_PAGES_FOR_PARALLEL = _int("PDF_MIN_PAGES_FOR_PARALLEL", 40)

# ---- Pagination ----
PAGE_SIZE = _int("PAGE_SIZE", 5000)

# ---- Download ----
MAX_DOWNLOAD_BYTES = _int("MAX_DOWNLOAD_BYTES", 100 * 1024 * 1024)

# ---- Queue (RabbitMQ) ----
RABBITMQ_URL = os.environ.get("RABBITMQ_URL", "amqp://guest:guest@localhost:5672/%2F")
RABBITMQ_EXCHANGE = os.environ.get("RABBITMQ_EXCHANGE", "markitdown")
RABBITMQ_QUEUE = os.environ.get("RABBITMQ_QUEUE", "markitdown.chunks")
# Unacked messages a worker may hold. 1 means a worker takes exactly one chunk
# at a time, which is what bounds its memory to a chunk rather than a document
# and keeps dispatch fair — without it RabbitMQ round-robins eagerly and a slow
# worker sits on a backlog while others idle.
RABBITMQ_PREFETCH = _int("RABBITMQ_PREFETCH", 1)
# How long a blocked consumer waits before yielding to heartbeats/shutdown.
RABBITMQ_CONSUME_TIMEOUT = _float("RABBITMQ_CONSUME_TIMEOUT", 5.0)
RABBITMQ_HEARTBEAT = _int("RABBITMQ_HEARTBEAT", 60)
# Delay tiers, in seconds, one retry queue each. Attempt N waits tier N.
#
# Per-tier queues rather than a per-message TTL on one queue: RabbitMQ only
# expires the message at the *head*, so a 300s message parked in front would
# hold back a 5s message queued behind it.
RETRY_DELAYS = [
    int(d) for d in os.environ.get("RETRY_DELAYS", "5,30,300").split(",") if d.strip()
]
MAX_ATTEMPTS = _int("MAX_ATTEMPTS", 3)
# Worker-local cache of downloaded source files, so a worker converting several
# chunks of the same document only fetches it from S3 once.
SOURCE_CACHE_DIR = os.environ.get("SOURCE_CACHE_DIR", "/tmp/markitdown-src")
SOURCE_CACHE_MAX_BYTES = _int("SOURCE_CACHE_MAX_BYTES", 2 * 1024 * 1024 * 1024)
# Touched by the worker loop each iteration; the k8s liveness probe checks its
# mtime to catch a wedged loop that is still technically running.
WORKER_LIVENESS_FILE = os.environ.get("WORKER_LIVENESS_FILE", "/tmp/worker-alive")

# ---- API ----
# Polling cadence used while the synchronous endpoint awaits its queued job.
SYNC_POLL_INTERVAL = _float("SYNC_POLL_INTERVAL", 0.5)
