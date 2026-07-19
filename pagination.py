"""
Splitting converted markdown into fixed-size pages.

Previously part of cache.py, which also owned Redis storage. Storage now lives
in storage.py; this is purely the page-splitting arithmetic.
"""
import config

PAGE_SIZE = config.PAGE_SIZE


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
