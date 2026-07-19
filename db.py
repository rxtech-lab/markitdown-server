"""
Database access: connections, migrations, and small query helpers.

Two interchangeable drivers sit behind the same DB-API 2.0 surface:

* **libsql** for hosted Turso (``libsql://…`` or any URL with an auth token).
* **stdlib sqlite3** for everything else — local development and CI.

Both speak the same SQL, so nothing above this module knows which is in use.
Keeping sqlite3 as the local/CI path means tests need no native libsql build
and no network, and the queue's claim semantics are exercised against a real
SQLite engine either way.

Connections are thread-local. Both entrypoints are multi-threaded — the API
runs blocking work in FastAPI's threadpool, and each worker runs a heartbeat
thread alongside its conversion — and neither driver's connection is safe to
share across threads.
"""
import logging
import os
import threading
from contextlib import contextmanager
from typing import Any, Iterable, Optional

import config

logger = logging.getLogger(__name__)

_local = threading.local()
_migrated = False
_migrate_lock = threading.Lock()

MIGRATIONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "migrations")

# How long a writer waits for a competing write lock before giving up. Workers
# claim chunks concurrently, so brief contention is normal and must block
# rather than raise "database is locked".
_BUSY_TIMEOUT_SECONDS = 30


def uses_libsql() -> bool:
    """
    True when the database is a libSQL *server* rather than a local SQLite file.

    Covers hosted Turso (libsql://) and a self-hosted sqld over plain HTTP,
    which is what the in-cluster deployment uses. Anything else — a path, or
    ":memory:" — is stdlib sqlite3.
    """
    url = config.DATABASE_URL
    return (
        url.startswith(("libsql://", "http://", "https://"))
        or bool(config.DATABASE_AUTH_TOKEN)
    )


def _connect_libsql():
    import libsql

    url = config.DATABASE_URL
    if url.startswith("file:"):
        url = url[len("file:"):]
    if config.DATABASE_AUTH_TOKEN:
        return libsql.connect(url, auth_token=config.DATABASE_AUTH_TOKEN)
    return libsql.connect(url)


def _connect_sqlite():
    import sqlite3

    url = config.DATABASE_URL
    if url.startswith("file:"):
        url = url[len("file:"):]

    if url == ":memory:":
        # A plain ":memory:" database is private to the connection that opened
        # it, so with thread-local connections every thread would silently get
        # its own empty database. Shared cache keeps them pointed at one.
        conn = sqlite3.connect(
            "file:markitdown-mem?mode=memory&cache=shared",
            uri=True,
            timeout=_BUSY_TIMEOUT_SECONDS,
        )
    else:
        conn = sqlite3.connect(url, timeout=_BUSY_TIMEOUT_SECONDS)
        # WAL lets readers proceed during a write, which matters because the
        # API polls job status while workers are claiming and completing.
        conn.execute("PRAGMA journal_mode=WAL")

    conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_SECONDS * 1000}")
    return conn


def get_connection():
    """Return this thread's connection, opening it on first use."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = _connect_libsql() if uses_libsql() else _connect_sqlite()
        _local.conn = conn
    return conn


def close_connection() -> None:
    """Close and forget this thread's connection."""
    conn = getattr(_local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            logger.debug("error closing connection", exc_info=True)
        _local.conn = None


def reset() -> None:
    """Drop the cached connection and re-run migrations on next use."""
    global _migrated
    close_connection()
    with _migrate_lock:
        _migrated = False


def migrate() -> None:
    """
    Apply every .sql file in migrations/ in name order.

    Migrations are written to be idempotent (IF NOT EXISTS), so this is safe to
    run from every pod on every start rather than needing a separate job.
    """
    global _migrated
    with _migrate_lock:
        if _migrated:
            return
        conn = get_connection()
        for name in sorted(os.listdir(MIGRATIONS_DIR)):
            if not name.endswith(".sql"):
                continue
            with open(os.path.join(MIGRATIONS_DIR, name), "r") as fh:
                sql = fh.read()
            for statement in _split_statements(sql):
                conn.execute(statement)
        conn.commit()
        _migrated = True
        logger.info("migrations applied")


def _split_statements(sql: str) -> list[str]:
    """
    Split a migration file into statements.

    Both drivers' execute() take one statement at a time. Line comments are
    stripped before splitting because a ';' inside a comment would otherwise
    cut a statement in half — which is exactly what "-- exclusive; -1 means
    the whole file" did.

    The migrations contain no string literals, so this does not need to be a
    real parser; if one is ever added, this must become quote-aware.
    """
    without_comments = "\n".join(
        line.split("--")[0] for line in sql.splitlines()
    )
    return [s.strip() for s in without_comments.split(";") if s.strip()]


def _rows_to_dicts(cursor) -> list[dict]:
    if cursor.description is None:
        return []
    # libsql returns None from fetchall() when a statement matched no rows,
    # where sqlite3 returns []. A RETURNING clause that matched nothing — the
    # normal "queue is empty" case — hits this on every poll.
    rows = cursor.fetchall()
    if not rows:
        return []
    columns = [d[0] for d in cursor.description]
    return [dict(zip(columns, row)) for row in rows]


# libsql speaks hrana over HTTP, where a connection holds a server-side stream
# handle that expires when idle ("Stream handle N is expired"). A pooled
# thread-local connection therefore goes stale on its own, and every subsequent
# statement on it fails until it is reopened. sqlite3 has no equivalent.
_STALE_CONNECTION_MARKERS = (
    "stream handle",
    "stream is closed",
    "stream not found",
    "connection reset",
    "connection closed",
    "broken pipe",
)


def _is_stale_connection(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(marker in message for marker in _STALE_CONNECTION_MARKERS)


def _run(sql: str, params: tuple, commit: bool) -> list[dict]:
    """
    Execute one statement, reopening the connection once if it went stale.

    The retry is deliberately narrow: it fires only on errors that mean the
    server never received the statement, so a write cannot be applied twice.
    Any other failure propagates untouched.
    """
    ensure_ready()
    for attempt in (1, 2):
        conn = get_connection()
        try:
            cursor = conn.execute(sql, params)
            rows = _rows_to_dicts(cursor)
            if commit:
                conn.commit()
            return rows
        except Exception as exc:
            if attempt == 1 and _is_stale_connection(exc):
                logger.info("reopening stale database connection: %s", exc)
                close_connection()
                continue
            raise
    return []


def query(sql: str, params: Iterable[Any] = ()) -> list[dict]:
    """Run a read and return rows as dicts."""
    return _run(sql, tuple(params), commit=False)


def query_one(sql: str, params: Iterable[Any] = ()) -> Optional[dict]:
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params: Iterable[Any] = ()) -> list[dict]:
    """
    Run a write and commit it.

    Returns any RETURNING rows, which is how the queue's claim and completion
    statements report what they actually changed.
    """
    return _run(sql, tuple(params), commit=True)


@contextmanager
def transaction():
    """
    Run a block of statements as one transaction.

    Yields the raw connection. Commits on clean exit, rolls back on exception.

    The connection is pinged first so an expired libsql stream handle is
    replaced *before* the block starts. A transaction cannot be retried
    transparently once the caller's statements have begun, so the cheap check
    up front is the only safe place to recover.
    """
    ensure_ready()
    query("SELECT 1 AS ok")
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            logger.warning("rollback failed", exc_info=True)
        raise


def changes(conn) -> int:
    """Rows modified by the most recent statement on ``conn``."""
    return conn.execute("SELECT changes() AS n").fetchall()[0][0]


def ensure_ready() -> None:
    if not _migrated:
        migrate()


def health() -> bool:
    try:
        # Goes through _run, so a connection that expired while the pod was
        # idle is reopened here rather than reported as a dead dependency.
        query("SELECT 1 AS ok")
        return True
    except Exception:
        logger.warning("database health check failed", exc_info=True)
        return False
