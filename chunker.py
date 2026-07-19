"""
Splitting a document into independently-convertible page ranges.

The producer only *plans* ranges — it never writes chunk files. Each worker
materialises its own range from the shared source, so no chunk bytes have to
travel between pods.
"""
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# A chunk covering the whole file, used for non-PDFs and small PDFs. -1 rather
# than a real page number because the worker must not try to page-slice a file
# format that has no pages.
WHOLE_FILE = (0, -1)


def page_count(path: str) -> Optional[int]:
    """
    Return the number of pages in ``path``, or None if it is not a readable PDF.

    Doubles as the PDF sniff, so a corrupt or non-PDF file transparently falls
    back to whole-file conversion.
    """
    try:
        from pypdf import PdfReader

        return len(PdfReader(path).pages)
    except Exception:
        return None


def plan_chunks(
    pages: Optional[int],
    pages_per_chunk: int,
    min_pages: int,
) -> list[tuple[int, int]]:
    """
    Plan the page ranges a document should be split into.

    Returns a list of ``(start, end)`` pairs with ``end`` exclusive, or a single
    ``WHOLE_FILE`` range when splitting is not applicable: a non-PDF (``pages``
    is None), or a PDF small enough that per-chunk parser startup would cost
    more than the parallelism saves.

    Pure and side-effect free so the partitioning can be tested without
    touching a real document.
    """
    if pages_per_chunk < 1:
        raise ValueError("pages_per_chunk must be >= 1")

    if pages is None or pages < min_pages:
        return [WHOLE_FILE]

    return [
        (start, min(start + pages_per_chunk, pages))
        for start in range(0, pages, pages_per_chunk)
    ]


def extract_pages(src_path: str, start: int, end: int, out_path: str) -> str:
    """
    Write pages ``[start, end)`` of ``src_path`` to ``out_path``.

    ``end == -1`` means the whole file, in which case the source is used
    directly rather than copied — the caller must not mutate or delete it.
    """
    if end == -1:
        return src_path

    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(src_path)
    writer = PdfWriter()
    for page in reader.pages[start:end]:
        writer.add_page(page)
    with open(out_path, "wb") as fh:
        writer.write(fh)
    return out_path
