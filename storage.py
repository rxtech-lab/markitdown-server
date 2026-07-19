"""
Content-addressed object storage for sources, chunk output and final pages.

Keys are derived from the file's content hash rather than its URL, so the same
document submitted from two different URLs converts once. The key also folds in
a fingerprint of every setting that changes the output, so bumping markitdown
or flipping the LLM flag invalidates cached results automatically instead of
serving stale markdown forever.

Because keys are content-addressed, writes are idempotent: a chunk that gets
converted twice (a lease expiring while its owner is still alive) writes
identical bytes to an identical key, so duplicates are harmless rather than
corrupting.
"""
import hashlib
import json
import logging
import threading
from typing import Optional

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError

import config

logger = logging.getLogger(__name__)

_local = threading.local()
_CHUNK_SIZE = 1024 * 1024


def get_client():
    """Return this thread's S3 client (boto3 clients are not thread-safe)."""
    client = getattr(_local, "client", None)
    if client is None:
        client = boto3.client(
            "s3",
            endpoint_url=config.S3_ENDPOINT,
            region_name=config.S3_REGION,
            aws_access_key_id=config.S3_ACCESS_KEY_ID,
            aws_secret_access_key=config.S3_SECRET_ACCESS_KEY,
            config=BotoConfig(retries={"max_attempts": 3, "mode": "standard"}),
        )
        _local.client = client
    return client


def reset() -> None:
    """Drop the cached client (used by tests)."""
    _local.client = None


# ---- Key derivation ----


def hash_file(path: str) -> str:
    """SHA-256 of a file's contents, streamed so memory stays bounded."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(_CHUNK_SIZE), b""):
            digest.update(block)
    return digest.hexdigest()


def config_fingerprint(use_llm: bool) -> str:
    """
    Fingerprint everything that changes conversion output.

    markitdown's version is included because an upgrade can legitimately
    produce different markdown for the same input; without it, cached results
    from the old version would be served indefinitely.
    """
    try:
        from importlib.metadata import version

        markitdown_version = version("markitdown")
    except Exception:
        markitdown_version = "unknown"

    parts = "|".join([
        markitdown_version,
        "llm" if use_llm else "nollm",
        config.OPENAI_MODEL if use_llm else "-",
        str(config.PAGE_SIZE),
        str(config.PAGES_PER_CHUNK),
        str(config.MIN_PAGES_FOR_PARALLEL),
    ])
    return hashlib.sha256(parts.encode()).hexdigest()[:16]


def make_doc_key(file_hash: str, use_llm: bool) -> str:
    return f"{file_hash}:{config_fingerprint(use_llm)}"


def source_key(file_hash: str) -> str:
    return f"src/{file_hash}"


def part_key(doc_key: str, chunk_index: int) -> str:
    return f"parts/{doc_key}/{chunk_index:05d}.md"


def manifest_key(doc_key: str) -> str:
    return f"docs/{doc_key}/manifest.json"


def page_key(doc_key: str, page: int) -> str:
    return f"docs/{doc_key}/pages/{page}.md"


# ---- Operations ----


def _get_bytes(key: str) -> Optional[bytes]:
    try:
        response = get_client().get_object(Bucket=config.S3_BUCKET, Key=key)
        return response["Body"].read()
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
            return None
        raise


def _put_bytes(key: str, data: bytes, content_type: str) -> None:
    get_client().put_object(
        Bucket=config.S3_BUCKET, Key=key, Body=data, ContentType=content_type
    )


def ensure_bucket() -> None:
    """
    Create the bucket if it does not exist.

    Idempotent and safe to call from every process on boot, which is what
    replaces the separate bucket-setup job. A pre-existing bucket (R2, S3, or a
    minio volume that survived a restart) short-circuits on the head_bucket.
    """
    client = get_client()
    try:
        client.head_bucket(Bucket=config.S3_BUCKET)
        return
    except ClientError:
        pass

    try:
        client.create_bucket(Bucket=config.S3_BUCKET)
        logger.info("created bucket %s", config.S3_BUCKET)
    except ClientError as exc:
        # Another process won the race, or the credentials are read-only
        # against a bucket that already exists. Both are fine.
        code = exc.response.get("Error", {}).get("Code")
        if code not in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
            raise


def exists(key: str) -> bool:
    try:
        get_client().head_object(Bucket=config.S3_BUCKET, Key=key)
        return True
    except ClientError:
        return False


def put_source(file_hash: str, path: str) -> str:
    """
    Upload a source file, skipping the transfer if it is already stored.

    Content-addressed, so an existing object with this key is by definition the
    same bytes.
    """
    key = source_key(file_hash)
    if exists(key):
        logger.info("source %s already stored", file_hash[:12])
        return key
    with open(path, "rb") as fh:
        get_client().put_object(Bucket=config.S3_BUCKET, Key=key, Body=fh)
    return key


def get_source(file_hash: str, dest_path: str) -> str:
    """Download a source file to ``dest_path``."""
    get_client().download_file(config.S3_BUCKET, source_key(file_hash), dest_path)
    return dest_path


def put_part(doc_key: str, chunk_index: int, markdown: str) -> str:
    key = part_key(doc_key, chunk_index)
    _put_bytes(key, markdown.encode("utf-8"), "text/markdown; charset=utf-8")
    return key


def get_part(doc_key: str, chunk_index: int) -> Optional[str]:
    data = _get_bytes(part_key(doc_key, chunk_index))
    return data.decode("utf-8") if data is not None else None


def put_document(doc_key: str, pages: list[str], total_length: int) -> dict:
    """
    Store every page plus a manifest, and return the manifest.

    Pages are written before the manifest so a manifest is never visible
    pointing at pages that do not exist yet.
    """
    for index, content in enumerate(pages, start=1):
        _put_bytes(
            page_key(doc_key, index),
            content.encode("utf-8"),
            "text/markdown; charset=utf-8",
        )

    manifest = {
        "doc_key": doc_key,
        "total_pages": len(pages),
        "total_length": total_length,
        "page_size": config.PAGE_SIZE,
    }
    _put_bytes(
        manifest_key(doc_key), json.dumps(manifest).encode("utf-8"), "application/json"
    )
    return manifest


def get_manifest(doc_key: str) -> Optional[dict]:
    data = _get_bytes(manifest_key(doc_key))
    return json.loads(data) if data is not None else None


def get_page(doc_key: str, page: int) -> Optional[str]:
    data = _get_bytes(page_key(doc_key, page))
    return data.decode("utf-8") if data is not None else None


def health() -> bool:
    try:
        get_client().head_bucket(Bucket=config.S3_BUCKET)
        return True
    except Exception:
        logger.warning("storage health check failed", exc_info=True)
        return False
