"""
Job and document state.

RabbitMQ distributes the work (see taskqueue.py); this owns everything the
broker cannot answer:

* the per-job remaining-chunk counter, which decides *who assembles* the
  finished document — a question that has to be answered atomically and
  exactly once across the whole worker fleet;
* progress for ``GET /convert/jobs/{id}``;
* the content-addressed document cache, so a re-submitted file skips
  conversion entirely.

``job_chunks`` mirrors each chunk's status for visibility and progress. It is
not the queue — RabbitMQ is — so it carries no leases, no claim, and no polling.
"""
import logging
import time
import uuid
from typing import Optional

import config
import db

logger = logging.getLogger(__name__)

# complete_chunk() sentinels.
JOB_ALREADY_TERMINAL = -1
CHUNK_ALREADY_DONE = -2


def now() -> int:
    return int(time.time())


def new_job_id() -> str:
    return uuid.uuid4().hex


# ---- Document cache ----


def find_cached_document(doc_key: str) -> Optional[dict]:
    """Return a previously converted document by cache key, if any."""
    return db.query_one("SELECT * FROM documents WHERE doc_key = ?", (doc_key,))


def record_document(
    doc_key: str,
    file_hash: str,
    total_pages: int,
    total_chunks: int,
    total_length: int,
    page_size: int,
) -> None:
    """
    Record a finished document.

    INSERT OR REPLACE rather than INSERT: two jobs for the same content can race
    (both submitted before either finished), and since the key is content
    addressed they produce identical output, so last writer wins harmlessly.
    """
    created = now()
    db.execute(
        """
        INSERT OR REPLACE INTO documents
            (doc_key, file_hash, config_fp, total_pages, total_chunks,
             total_length, page_size, created_at, expires_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            doc_key,
            file_hash,
            doc_key.split(":")[-1],
            total_pages,
            total_chunks,
            total_length,
            page_size,
            created,
            created + config.DOC_TTL_DAYS * 86400,
        ),
    )


# ---- Job lifecycle ----


def create_job(
    job_id: str,
    doc_key: str,
    file_hash: str,
    url: str,
    ranges: list[tuple[int, int]],
    use_llm: bool,
) -> None:
    """
    Create a job and its chunk rows.

    Inserting the chunk rows *is* the enqueue — there is no separate publish
    step, so a job can never exist with its work missing from the queue.
    """
    timestamp = now()
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO jobs
                (job_id, doc_key, file_hash, url, status, total_chunks,
                 remaining_chunks, use_llm, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?)
            """,
            (job_id, doc_key, file_hash, url, len(ranges), len(ranges),
             1 if use_llm else 0, timestamp, timestamp),
        )
        for index, (start, end) in enumerate(ranges):
            conn.execute(
                """
                INSERT INTO job_chunks
                    (job_id, chunk_index, start_page, end_page, status,
                     attempts, available_at, updated_at)
                VALUES (?, ?, ?, ?, 'pending', 0, ?, ?)
                """,
                (job_id, index, start, end, timestamp, timestamp),
            )
    logger.info("created job %s with %d chunks", job_id, len(ranges))


def get_job(job_id: str) -> Optional[dict]:
    return db.query_one("SELECT * FROM jobs WHERE job_id = ?", (job_id,))


def job_progress(job_id: str) -> dict:
    job = get_job(job_id)
    if job is None:
        return {"completed": 0, "total": 0, "percent": 0}
    total = job["total_chunks"]
    completed = total - job["remaining_chunks"]
    return {
        "completed": completed,
        "total": total,
        "percent": round(100 * completed / total) if total else 0,
    }


def finish_job(job_id: str) -> None:
    db.execute(
        "UPDATE jobs SET status = 'done', updated_at = ? WHERE job_id = ?",
        (now(), job_id),
    )


def fail_job(job_id: str, error: str, chunk_index: Optional[int] = None) -> None:
    """
    Mark a job failed.

    One poison chunk fails the whole job: silently returning a document that is
    missing twenty pages is worse than returning an error. Sibling chunks still
    in flight discover this via complete_chunk() and drop their work.
    """
    db.execute(
        """
        UPDATE jobs SET status = 'failed', error = ?, error_chunk = ?, updated_at = ?
        WHERE job_id = ? AND status NOT IN ('done', 'failed')
        """,
        (error[:2000], chunk_index, now(), job_id),
    )
    logger.warning("job %s failed: %s", job_id, error)


# ---- Chunk state ----


def start_chunk(job_id: str, chunk_index: int, owner: str, attempt: int) -> None:
    """
    Record that a worker has picked a chunk up.

    Bookkeeping for progress and debugging only — the broker, not this row,
    decides who holds the message.
    """
    timestamp = now()
    db.execute(
        """
        UPDATE job_chunks
        SET status = 'running', lease_owner = ?, attempts = ?, updated_at = ?
        WHERE job_id = ? AND chunk_index = ? AND status <> 'done'
        """,
        (owner, attempt, timestamp, job_id, chunk_index),
    )
    db.execute(
        """
        UPDATE jobs SET status = 'running', updated_at = ?
        WHERE job_id = ? AND status = 'queued'
        """,
        (timestamp, job_id),
    )


def fail_chunk(job_id: str, chunk_index: int, error: str) -> None:
    """Mark a chunk as permanently failed after exhausting its attempts."""
    db.execute(
        """
        UPDATE job_chunks
        SET status = 'failed', lease_owner = NULL, last_error = ?, updated_at = ?
        WHERE job_id = ? AND chunk_index = ?
        """,
        (error[:2000], now(), job_id, chunk_index),
    )


def reschedule_chunk(job_id: str, chunk_index: int, error: str) -> None:
    """Mark a chunk as awaiting retry; RabbitMQ owns the actual redelivery."""
    db.execute(
        """
        UPDATE job_chunks
        SET status = 'pending', lease_owner = NULL, last_error = ?, updated_at = ?
        WHERE job_id = ? AND chunk_index = ? AND status <> 'done'
        """,
        (error[:2000], now(), job_id, chunk_index),
    )


def complete_chunk(job_id: str, chunk_index: int, s3_key: str) -> int:
    """
    Record a finished chunk and return the job's remaining chunk count.

    Returns 0 when this caller completed the *last* chunk and is therefore
    responsible for assembling the document, ``CHUNK_ALREADY_DONE`` if this
    chunk was already recorded, or ``JOB_ALREADY_TERMINAL`` if the job has
    since finished or failed.

    The guarded first UPDATE is what makes this idempotent, and that is load
    bearing: RabbitMQ delivers at least once, so a worker that converts a chunk
    and dies before acking will have it redelivered and converted again.
    Without the guard, ``remaining_chunks`` would be decremented twice, drive
    past zero, and no worker would ever see 0 to assemble the document.
    """
    timestamp = now()
    with db.transaction() as conn:
        conn.execute(
            """
            UPDATE job_chunks
            SET status = 'done', s3_key = ?, lease_owner = NULL,
                last_error = NULL, updated_at = ?
            WHERE job_id = ? AND chunk_index = ? AND status <> 'done'
            """,
            (s3_key, timestamp, job_id, chunk_index),
        )
        if db.changes(conn) == 0:
            return CHUNK_ALREADY_DONE

        cursor = conn.execute(
            """
            UPDATE jobs
            SET remaining_chunks = remaining_chunks - 1,
                status = 'running',
                updated_at = ?
            WHERE job_id = ? AND status NOT IN ('done', 'failed')
            RETURNING remaining_chunks
            """,
            (timestamp, job_id),
        )
        rows = cursor.fetchall()
        if not rows:
            return JOB_ALREADY_TERMINAL
        return rows[0][0]


def chunk_indices(job_id: str) -> list[int]:
    """Every chunk index for a job, in page order."""
    rows = db.query(
        "SELECT chunk_index FROM job_chunks WHERE job_id = ? ORDER BY chunk_index",
        (job_id,),
    )
    return [row["chunk_index"] for row in rows]
