import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Optional

import yaml
from fastapi import Depends, FastAPI, HTTPException, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field

import chunker
import config
import converter
import db
import jobstore
import pagination
import storage
import taskqueue

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Migrations are idempotent (IF NOT EXISTS), so every API pod can run them
    # on boot. Object-storage provisioning belongs to deployment setup, not the
    # request-serving process; readiness reports when that dependency is down.
    await run_in_threadpool(db.migrate)
    yield


app = FastAPI(
    lifespan=lifespan,
    title="markitdown-server",
    version="1.0.0",
    description=(
        "Convert documents at a URL to paginated markdown.\n\n"
        "`POST /convert` preserves the synchronous API and returns only after "
        "conversion finishes. `POST /async/convert` returns a `job_id` to poll "
        "at `GET /convert/jobs/{job_id}`. Both routes use the same distributed "
        "worker queue, and finished pages are read from "
        "`GET /convert/{doc_key}/pages/{page}`. Conversion is content-addressed "
        "on the source bytes plus `use_llm`, so re-submitting an identical file "
        "skips the queue and returns its first page directly."
    ),
)


class ConvertRequest(BaseModel):
    file: Optional[str] = Field(
        None, description="URL of the document to convert. Fetched by the server.",
        examples=["https://example.com/book.pdf"],
    )
    # Whether to run markitdown's LLM-assisted conversion. Folded into the
    # cache key, so LLM and non-LLM results are stored separately.
    use_llm: Optional[bool] = Field(
        None,
        description=(
            "Run markitdown's LLM-assisted conversion (image descriptions). "
            "Part of the cache key, so results do not collide with non-LLM "
            "ones. Defaults to the server's CONVERT_USE_LLM."
        ),
    )


class Pagination(BaseModel):
    id: str
    page: int
    page_size: int
    total_pages: int
    total_length: int
    has_next: bool
    has_prev: bool
    next_page: Optional[int] = Field(None, description="null on the last page.")
    prev_page: Optional[int] = Field(None, description="null on the first page.")


class PageResponse(BaseModel):
    """One page of converted markdown. Also the 200 body of `POST /convert`."""
    id: str = Field(description="The document's `doc_key`.")
    content: str
    pagination: Pagination


class JobAccepted(BaseModel):
    """202 body of `POST /async/convert`: work was queued."""
    job_id: str
    status: str = Field(examples=["queued", "running"])
    doc_key: str
    total_chunks: int


class JobProgress(BaseModel):
    completed: int
    total: int
    percent: int


class JobStatus(BaseModel):
    job_id: str
    status: str = Field(
        description="queued -> running -> done | failed.",
        examples=["queued", "running", "done", "failed"],
    )
    doc_key: str
    progress: JobProgress
    error: Optional[str] = Field(None, description="Set only when status is failed.")
    created_at: int = Field(description="Unix epoch seconds.")
    updated_at: int = Field(description="Unix epoch seconds.")


class Health(BaseModel):
    status: str = Field(examples=["ok"])


class Readiness(BaseModel):
    status: str = Field(examples=["ok", "degraded"])
    checks: dict[str, bool] = Field(
        description="Per-dependency health: database, storage, queue."
    )


# auto_error=False so a missing header falls through to the check below and
# yields 401 rather than the 403 the security scheme would raise on its own.
api_key_header = APIKeyHeader(name="x-api-key", auto_error=False)


async def verify_api_key(x_api_key: str = Depends(api_key_header)):
    admin_api_key = config.ADMIN_API_KEY
    if not admin_api_key:
        raise HTTPException(status_code=500, detail="API key not configured on server")

    if x_api_key != admin_api_key:
        raise HTTPException(status_code=401, detail="Invalid API key")

    return x_api_key


def _first_page_payload(doc_key: str, manifest: dict) -> Optional[dict]:
    content = storage.get_page(doc_key, 1)
    if content is None:
        return None
    return {
        "id": doc_key,
        "content": content,
        "pagination": pagination.build_pagination(
            doc_key, 1, manifest["total_pages"],
            manifest["total_length"], manifest["page_size"],
        ),
    }


def submit(url: str, use_llm: bool) -> dict:
    """
    Producer: hash the source, then either serve it from cache or enqueue work.

    Blocking (network + hashing); callers on the event loop must offload this.
    """
    started = time.monotonic()
    path = converter.download(url)
    try:
        file_hash = storage.hash_file(path)
        doc_key = storage.make_doc_key(file_hash, use_llm)

        # Content-addressed, so an existing document for this key was produced
        # from identical bytes under identical settings. Nothing to do.
        cached = jobstore.find_cached_document(doc_key)
        if cached is not None:
            manifest = storage.get_manifest(doc_key)
            if manifest is not None:
                payload = _first_page_payload(doc_key, manifest)
                if payload is not None:
                    logger.info("cache hit for %s (%.2fs)", url, time.monotonic() - started)
                    return {"cached": True, "payload": payload}
            # Row survived its objects (lifecycle expiry). Reconvert.
            logger.info("stale document row for %s, reconverting", doc_key)

        pages = chunker.page_count(path)
        ranges = chunker.plan_chunks(
            pages, config.PAGES_PER_CHUNK, config.MIN_PAGES_FOR_PARALLEL
        )
        storage.put_source(file_hash, path)
    finally:
        if os.path.exists(path):
            os.remove(path)

    job_id = jobstore.new_job_id()
    # State first, then publish. A message whose job row does not exist yet
    # would be delivered to a worker that cannot find it; the reverse — rows
    # with no message — is merely a job that never starts, which is visible in
    # the status endpoint rather than a confusing worker-side error.
    jobstore.create_job(job_id, doc_key, file_hash, url, ranges, use_llm)
    taskqueue.publish([
        taskqueue.ChunkTask(
            job_id=job_id,
            chunk_index=index,
            doc_key=doc_key,
            file_hash=file_hash,
            start_page=start,
            end_page=end,
            total_chunks=len(ranges),
            use_llm=use_llm,
        )
        for index, (start, end) in enumerate(ranges)
    ])
    logger.info(
        "queued job %s for %s: %s pages -> %d chunks (%.2fs)",
        job_id, url, pages, len(ranges), time.monotonic() - started,
    )
    return {"cached": False, "job_id": job_id, "doc_key": doc_key,
            "total_chunks": len(ranges)}


def _job_payload(job: dict) -> dict:
    return {
        "job_id": job["job_id"],
        "status": job["status"],
        "doc_key": job["doc_key"],
        "progress": jobstore.job_progress(job["job_id"]),
        "error": job["error"],
        "created_at": job["created_at"],
        "updated_at": job["updated_at"],
    }


async def _wait_for_job(job_id: str) -> dict:
    """Poll a queued job until the worker fleet finishes or fails it."""
    while True:
        job = await run_in_threadpool(jobstore.get_job, job_id)
        if job is not None and job["status"] in ("done", "failed"):
            return job
        await asyncio.sleep(config.SYNC_POLL_INTERVAL)


async def _submit_request(request: Optional[ConvertRequest]) -> dict:
    """Validate and enqueue a request, translating producer errors to HTTP."""
    if request is None or not request.file:
        raise HTTPException(status_code=422, detail="`file` (a URL) is required")

    use_llm = config.CONVERT_USE_LLM if request.use_llm is None else request.use_llm

    try:
        return await run_in_threadpool(submit, request.file, use_llm)
    except Exception as exc:
        # Download and size-cap failures are the caller's problem and are
        # reported synchronously, rather than as a job that instantly fails.
        logger.exception("submission failed for %s", request.file)
        raise HTTPException(
            status_code=400, detail=f"Error processing URL: {str(exc)}"
        ) from exc


async def _completed_job_payload(job: dict) -> dict:
    """Turn a successful terminal job into the original first-page response."""
    if job["status"] == "failed":
        raise HTTPException(
            status_code=400, detail=f"Conversion failed: {job['error']}"
        )

    manifest = await run_in_threadpool(storage.get_manifest, job["doc_key"])
    if manifest is None:
        raise HTTPException(status_code=500, detail="Converted document is missing")
    payload = await run_in_threadpool(_first_page_payload, job["doc_key"], manifest)
    if payload is None:
        raise HTTPException(status_code=500, detail="Converted document is missing")
    return payload


@app.post(
    "/convert",
    dependencies=[Depends(verify_api_key)],
    summary="Convert a document synchronously",
    response_model=PageResponse,
    responses={
        200: {"description": "Conversion finished: first page."},
        400: {"description": "The URL could not be downloaded, or conversion failed."},
        422: {"description": "`file` is missing."},
    },
)
async def convert_endpoint(
        request: Optional[ConvertRequest] = None,
):
    """
    Preserve the original blocking API while distributing work to worker pods.

    This request returns only after every chunk has finished and the document
    has been assembled. Callers that want a job id immediately should use
    ``POST /async/convert`` instead.
    """
    result = await _submit_request(request)

    if result["cached"]:
        return result["payload"]

    job = await _wait_for_job(result["job_id"])
    return await _completed_job_payload(job)


@app.post(
    "/async/convert",
    dependencies=[Depends(verify_api_key)],
    summary="Submit a document for asynchronous conversion",
    responses={
        200: {"model": PageResponse, "description": "Cache hit: first page."},
        202: {"model": JobAccepted, "description": "Queued; poll the job id."},
        400: {"description": "The URL could not be downloaded."},
        422: {"description": "`file` is missing."},
    },
)
async def async_convert_endpoint(
        response: Response,
        request: Optional[ConvertRequest] = None,
):
    """Queue conversion and return immediately unless the document is cached."""
    result = await _submit_request(request)

    if result["cached"]:
        return result["payload"]

    response.status_code = 202
    return {
        "job_id": result["job_id"],
        "status": "queued",
        "doc_key": result["doc_key"],
        "total_chunks": result["total_chunks"],
    }


@app.get(
    "/convert/jobs/{job_id}",
    dependencies=[Depends(verify_api_key)],
    summary="Poll a conversion job",
    response_model=JobStatus,
    responses={404: {"description": "No such job."}},
)
async def get_job_endpoint(job_id: str):
    """Status and progress for a submitted conversion job."""
    job = await run_in_threadpool(jobstore.get_job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return await run_in_threadpool(_job_payload, job)


@app.get(
    "/convert/{doc_id}/pages/{page}",
    dependencies=[Depends(verify_api_key)],
    summary="Read one page of a converted document",
    response_model=PageResponse,
    responses={404: {"description": "Unknown document, expired, or page out of range."}},
)
async def get_page_endpoint(doc_id: str, page: int):
    """
    Fetch a single page of a previously converted document.

    Returns 404 if the document has expired from storage or the page is out
    of range.
    """
    manifest = await run_in_threadpool(storage.get_manifest, doc_id)
    if manifest is None or page < 1 or page > manifest["total_pages"]:
        raise HTTPException(
            status_code=404,
            detail="Document or page not found (it may have expired).",
        )

    content = await run_in_threadpool(storage.get_page, doc_id, page)
    if content is None:
        raise HTTPException(
            status_code=404,
            detail="Document or page not found (it may have expired).",
        )

    return {
        "id": doc_id,
        "content": content,
        "pagination": pagination.build_pagination(
            doc_id, page, manifest["total_pages"],
            manifest["total_length"], manifest["page_size"],
        ),
    }


_openapi_yaml: Optional[str] = None


@app.get("/openapi.yaml", include_in_schema=False)
async def openapi_yaml():
    """
    The generated OpenAPI document, as YAML.

    Serialised from `app.openapi()` rather than kept as a checked-in file, so
    it cannot drift from the routes. FastAPI already serves the JSON form at
    `/openapi.json`; this is the same document for tooling that wants YAML.
    """
    global _openapi_yaml
    if _openapi_yaml is None:
        # app.openapi() memoises its dict, so this only pays the dump cost once.
        _openapi_yaml = yaml.safe_dump(app.openapi(), sort_keys=False)
    return Response(_openapi_yaml, media_type="application/yaml")


@app.get("/", response_model=Health, summary="Liveness")
async def root():
    """Liveness. Deliberately checks nothing external — a database or storage
    blip should not get the pod restarted."""
    return {"status": "ok"}


@app.get(
    "/readyz",
    response_model=Readiness,
    summary="Readiness",
    responses={503: {"model": Readiness, "description": "A dependency is down."}},
)
async def readyz(response: Response):
    """Readiness. Fails the pod out of the Service while dependencies are down."""
    checks = {
        "database": await run_in_threadpool(db.health),
        "storage": await run_in_threadpool(storage.health),
        "queue": await run_in_threadpool(taskqueue.health),
    }
    if not all(checks.values()):
        response.status_code = 503
    return {"status": "ok" if all(checks.values()) else "degraded", "checks": checks}
