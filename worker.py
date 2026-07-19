"""
The consumer. Converts chunks delivered by RabbitMQ and assembles finished
documents.

Run with ``python -m worker``. Scale by adding pods: RabbitMQ dispatches each
chunk to whichever worker is free, so there is no leader, no partition
assignment, and nothing to reconfigure when the fleet size changes.

Each worker holds one unacked message at a time (prefetch=1). That bounds
memory to the size of a *chunk* rather than a document, and keeps dispatch fair
— a worker stuck on a slow chunk stops receiving new ones instead of hoarding a
backlog while its peers idle.
"""
import logging
import os
import signal
import socket
import tempfile
import threading
import time

import chunker
import config
import converter
import jobstore
import pagination
import storage
import taskqueue
from taskqueue import ChunkTask

logger = logging.getLogger(__name__)

_stop = threading.Event()


def consumer_name() -> str:
    return f"{socket.gethostname()}-{os.getpid()}"


def _touch_liveness() -> None:
    """
    Mark the loop as alive for the k8s liveness probe.

    The worker serves no HTTP, so the probe checks this file's mtime instead.
    Touched on every consume tick, including idle ones.
    """
    try:
        with open(config.WORKER_LIVENESS_FILE, "w") as fh:
            fh.write(str(int(time.time())))
    except OSError:
        logger.debug("could not touch liveness file", exc_info=True)


def fetch_source(file_hash: str) -> str:
    """
    Get the source file locally, caching it under SOURCE_CACHE_DIR.

    A worker often receives several chunks of the same document, so caching
    turns N downloads into one.
    """
    os.makedirs(config.SOURCE_CACHE_DIR, exist_ok=True)
    path = os.path.join(config.SOURCE_CACHE_DIR, file_hash)
    if os.path.exists(path):
        os.utime(path, None)  # mark as recently used for the LRU prune
        return path

    # Download to a temp name and rename, so a crashed download cannot leave a
    # truncated file that later looks like a valid cache hit.
    fd, temp_path = tempfile.mkstemp(dir=config.SOURCE_CACHE_DIR)
    os.close(fd)
    try:
        storage.get_source(file_hash, temp_path)
        os.replace(temp_path, path)
    except Exception:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        raise

    prune_source_cache()
    return path


def prune_source_cache(max_bytes: int = None) -> None:
    """Evict least-recently-used sources until the cache fits in its budget."""
    max_bytes = config.SOURCE_CACHE_MAX_BYTES if max_bytes is None else max_bytes
    try:
        entries = []
        total = 0
        for name in os.listdir(config.SOURCE_CACHE_DIR):
            full = os.path.join(config.SOURCE_CACHE_DIR, name)
            if not os.path.isfile(full):
                continue
            stat = os.stat(full)
            entries.append((stat.st_atime, stat.st_size, full))
            total += stat.st_size

        for _, size, full in sorted(entries):
            if total <= max_bytes:
                break
            os.remove(full)
            total -= size
    except OSError:
        logger.debug("source cache prune failed", exc_info=True)


def convert_range(file_hash: str, start: int, end: int, use_llm: bool) -> str:
    """Materialise one page range from the shared source and convert it."""
    source_path = fetch_source(file_hash)

    if end == -1:
        # Whole-file chunk (non-PDF, or a PDF too small to split).
        return converter.convert_file(source_path, use_llm)

    fd, chunk_path = tempfile.mkstemp(suffix=".pdf")
    os.close(fd)
    try:
        chunker.extract_pages(source_path, start, end, chunk_path)
        return converter.convert_file(chunk_path, use_llm)
    finally:
        if os.path.exists(chunk_path):
            os.remove(chunk_path)


def assemble(job: dict) -> None:
    """
    Join every chunk's markdown into the final document and publish it.

    Called by whichever worker completed the last chunk. Parts are fetched in
    chunk-index order, which is page order.
    """
    job_id = job["job_id"]
    doc_key = job["doc_key"]
    logger.info("assembling job %s (%d chunks)", job_id, job["total_chunks"])

    parts = []
    for index in jobstore.chunk_indices(job_id):
        part = storage.get_part(doc_key, index)
        if part is None:
            raise RuntimeError(f"chunk {index} missing from storage")
        parts.append(part)

    markdown = "\n\n".join(part for part in parts if part and part.strip())
    pages = pagination.paginate(markdown, config.PAGE_SIZE)

    storage.put_document(doc_key, pages, len(markdown))
    jobstore.record_document(
        doc_key=doc_key,
        file_hash=job["file_hash"],
        total_pages=len(pages),
        total_chunks=job["total_chunks"],
        total_length=len(markdown),
        page_size=config.PAGE_SIZE,
    )
    jobstore.finish_job(job_id)
    logger.info("job %s done: %d pages, %d chars", job_id, len(pages), len(markdown))


def handle(task: ChunkTask) -> None:
    """
    Convert one delivered chunk.

    Never raises: the message is acked by the consumer loop once this returns,
    so any retry decision has to be made here and republished explicitly.
    """
    owner = consumer_name()
    job = jobstore.get_job(task.job_id)
    if job is None or job["status"] in ("done", "failed"):
        logger.info("dropping chunk %d: job %s is no longer active",
                    task.chunk_index, task.job_id)
        return

    jobstore.start_chunk(task.job_id, task.chunk_index, owner, task.attempt)
    started = time.monotonic()

    try:
        markdown = convert_range(
            task.file_hash, task.start_page, task.end_page, task.use_llm
        )
        key = storage.put_part(task.doc_key, task.chunk_index, markdown)
    except Exception as exc:
        _handle_failure(task, exc)
        return

    remaining = jobstore.complete_chunk(task.job_id, task.chunk_index, key)
    logger.info(
        "converted chunk %d/%d of job %s in %.1fs (%d remaining)",
        task.chunk_index + 1, task.total_chunks, task.job_id,
        time.monotonic() - started, max(remaining, 0),
    )

    if remaining == 0:
        try:
            assemble(jobstore.get_job(task.job_id))
        except Exception as exc:
            logger.exception("assembly failed for job %s", task.job_id)
            jobstore.fail_job(task.job_id, f"assembly failed: {exc}")


def _handle_failure(task: ChunkTask, exc: Exception) -> None:
    """Retry a failed chunk, or fail the whole job once attempts run out."""
    error = f"{type(exc).__name__}: {exc}"
    logger.exception("chunk %d of job %s failed (attempt %d/%d)",
                     task.chunk_index, task.job_id, task.attempt,
                     config.MAX_ATTEMPTS)

    if task.attempt >= config.MAX_ATTEMPTS:
        # One poison chunk fails the job: silently returning a document that is
        # missing pages is worse than returning an error.
        taskqueue.publish_failed(task, error)
        jobstore.fail_chunk(task.job_id, task.chunk_index, error)
        jobstore.fail_job(
            task.job_id,
            f"chunk {task.chunk_index} failed after {task.attempt} attempts: {exc}",
            task.chunk_index,
        )
        return

    jobstore.reschedule_chunk(task.job_id, task.chunk_index, error)
    taskqueue.publish_retry(task)


def run_forever() -> None:
    owner = consumer_name()
    logger.info("worker %s consuming from %s", owner, config.RABBITMQ_QUEUE)

    def on_task(task: ChunkTask) -> None:
        _touch_liveness()
        try:
            handle(task)
        except Exception:
            # handle() is meant to absorb its own errors; anything escaping
            # here would otherwise kill the consumer loop.
            logger.exception("unhandled error on chunk %d of job %s",
                             task.chunk_index, task.job_id)

    while not _stop.is_set():
        try:
            _touch_liveness()
            taskqueue.consume(on_task, _stop.is_set)
        except Exception:
            if _stop.is_set():
                break
            # A dropped broker connection is expected during a RabbitMQ
            # restart; reconnect rather than crashlooping the pod.
            logger.exception("consumer connection lost, reconnecting")
            taskqueue.close()
            _stop.wait(5)

    taskqueue.close()
    logger.info("worker %s stopped", owner)


def _handle_signal(signum, _frame) -> None:
    """
    Stop after the current chunk.

    The in-flight message is deliberately not nacked: if the process is killed
    before it finishes, the unacked message is redelivered by the broker
    automatically, which is crash-safe in a way a signal handler is not.
    """
    logger.info("received signal %s, finishing current chunk", signum)
    _stop.set()


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    import db

    db.migrate()
    storage.ensure_bucket()
    run_forever()


if __name__ == "__main__":
    main()
