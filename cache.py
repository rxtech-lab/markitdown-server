import json
import os
import uuid
from typing import Optional

import redis

# ---- Configuration (overridable via env) ----
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
# Number of characters per page.
PAGE_SIZE = int(os.environ.get("PAGE_SIZE", "5000"))
# How long (seconds) paginated content lives in Redis.
CACHE_TTL = int(os.environ.get("CACHE_TTL", "300"))  # 5 minutes

_KEY_PREFIX = "markitdown:doc"

_client: Optional[redis.Redis] = None


def get_client() -> redis.Redis:
    """Return a lazily-initialised, shared Redis client."""
    global _client
    if _client is None:
        _client = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    return _client


def _meta_key(doc_id: str) -> str:
    return f"{_KEY_PREFIX}:{doc_id}:meta"


def _page_key(doc_id: str, page: int) -> str:
    return f"{_KEY_PREFIX}:{doc_id}:page:{page}"


def paginate(content: str, page_size: int = PAGE_SIZE) -> list[str]:
    """
    Split markdown content into pages of roughly ``page_size`` characters.

    Pages are broken on paragraph boundaries (``\\n\\n``) where possible so a
    paragraph is not split across pages. A single paragraph larger than
    ``page_size`` is hard-split.
    """
    if not content:
        return [""]

    pages: list[str] = []
    current = ""

    for block in content.split("\n\n"):
        # Hard-split a single block that is itself larger than a page.
        while len(block) > page_size:
            if current:
                pages.append(current)
                current = ""
            pages.append(block[:page_size])
            block = block[page_size:]

        candidate = block if not current else f"{current}\n\n{block}"
        if len(candidate) > page_size:
            pages.append(current)
            current = block
        else:
            current = candidate

    if current or not pages:
        pages.append(current)

    return pages


def build_pagination(doc_id: str, page: int, total_pages: int, total_length: int,
                     page_size: int = PAGE_SIZE) -> dict:
    has_next = page < total_pages
    has_prev = page > 1
    return {
        "id": doc_id,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
        "total_length": total_length,
        "has_next": has_next,
        "has_prev": has_prev,
        "next_page": page + 1 if has_next else None,
        "prev_page": page - 1 if has_prev else None,
    }


def store_document(content: str, page_size: int = PAGE_SIZE,
                   ttl: int = CACHE_TTL) -> dict:
    """
    Paginate ``content`` and store every page + metadata in Redis with a TTL.

    Returns a payload for the first page::

        {"id", "content", "pagination": {...}}
    """
    pages = paginate(content, page_size)
    doc_id = uuid.uuid4().hex
    total_pages = len(pages)
    total_length = len(content)

    meta = {
        "total_pages": total_pages,
        "total_length": total_length,
        "page_size": page_size,
    }

    client = get_client()
    pipe = client.pipeline()
    pipe.set(_meta_key(doc_id), json.dumps(meta), ex=ttl)
    for idx, page_content in enumerate(pages, start=1):
        pipe.set(_page_key(doc_id, idx), page_content, ex=ttl)
    pipe.execute()

    return {
        "id": doc_id,
        "content": pages[0],
        "pagination": build_pagination(doc_id, 1, total_pages, total_length, page_size),
    }


def get_page(doc_id: str, page: int) -> Optional[dict]:
    """
    Fetch a single page for a previously stored document.

    Returns ``None`` if the document/page is not found (expired or invalid).
    """
    client = get_client()
    raw_meta = client.get(_meta_key(doc_id))
    if raw_meta is None:
        return None

    meta = json.loads(raw_meta)
    total_pages = meta["total_pages"]
    if page < 1 or page > total_pages:
        return None

    content = client.get(_page_key(doc_id, page))
    if content is None:
        return None

    return {
        "id": doc_id,
        "content": content,
        "pagination": build_pagination(
            doc_id, page, total_pages, meta["total_length"], meta["page_size"]
        ),
    }
