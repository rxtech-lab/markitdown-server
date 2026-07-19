"""
Page-chunked, multi-process PDF conversion.

markitdown's ``PdfConverter`` holds the parsed pdfplumber object graph for the
*whole* document at once (and, for prose PDFs, then re-parses everything with
pdfminer), so both peak memory and wall-clock grow with document size in a
single process.

Splitting the PDF into page ranges and converting each range in its own process
bounds both: a worker only ever holds N pages of parsed objects, and it returns
that memory to the OS when it exits.

Both pdfplumber and pdfminer.six are pure Python, so this has to use processes —
threads would serialise on the GIL and buy nothing.
"""
import logging
import os
import shutil
import tempfile
from concurrent.futures import ProcessPoolExecutor
from typing import Optional

logger = logging.getLogger(__name__)

# Pages per chunk. Smaller chunks lower peak memory per worker but add
# per-chunk parser startup overhead.
PAGES_PER_CHUNK = int(os.environ.get("PDF_PAGES_PER_CHUNK", "20"))
# Worker processes. Each holds ~140-190 MiB just for the markitdown import, so
# this must be sized against the container memory limit, not just its CPU.
MAX_WORKERS = int(os.environ.get("PDF_MAX_WORKERS", "2"))
# Below this page count the process startup cost outweighs the parallelism.
MIN_PAGES_FOR_PARALLEL = int(os.environ.get("PDF_MIN_PAGES_FOR_PARALLEL", "40"))


def page_count(path: str) -> Optional[int]:
    """
    Return the number of pages in ``path``, or None if it is not a readable PDF.

    Used both to decide whether chunking is worthwhile and as the PDF sniff, so
    a corrupt or non-PDF file transparently falls back to the normal path.
    """
    try:
        from pypdf import PdfReader

        return len(PdfReader(path).pages)
    except Exception:
        return None


def split_pdf(path: str, pages_per_chunk: int, out_dir: str) -> list[str]:
    """
    Split ``path`` into single-file PDFs of ``pages_per_chunk`` pages each.

    Chunks are written to ``out_dir`` and passed to workers by path rather than
    by value — pickling the bytes through the process pool would duplicate the
    document in memory, which is the thing this module exists to avoid.
    """
    from pypdf import PdfReader, PdfWriter

    if pages_per_chunk < 1:
        raise ValueError("pages_per_chunk must be >= 1")

    reader = PdfReader(path)
    total = len(reader.pages)
    chunk_paths: list[str] = []

    for index, start in enumerate(range(0, total, pages_per_chunk)):
        writer = PdfWriter()
        for page in reader.pages[start:start + pages_per_chunk]:
            writer.add_page(page)
        chunk_path = os.path.join(out_dir, f"chunk_{index:05d}.pdf")
        with open(chunk_path, "wb") as fh:
            writer.write(fh)
        chunk_paths.append(chunk_path)

    return chunk_paths


def convert_chunk(chunk_path: str) -> str:
    """
    Convert one chunk file to markdown. Runs in a worker process.

    Must stay importable at module level so the pool can pickle it. markitdown
    is imported lazily here so the parent does not pay for it when the chunked
    path is never taken.
    """
    from markitdown._stream_info import StreamInfo
    from markitdown.converters._pdf_converter import PdfConverter

    stream_info = StreamInfo(extension=".pdf", mimetype="application/pdf")
    with open(chunk_path, "rb") as fh:
        return PdfConverter().convert(fh, stream_info).markdown


def convert_pdf_chunked(
    path: str,
    pages_per_chunk: int = PAGES_PER_CHUNK,
    max_workers: int = MAX_WORKERS,
) -> str:
    """
    Convert a PDF by splitting it into page ranges converted in parallel.

    Chunk results are concatenated in page order, so output ordering matches a
    whole-document conversion.
    """
    work_dir = tempfile.mkdtemp(prefix="pdf_chunks_")
    try:
        chunk_paths = split_pdf(path, pages_per_chunk, work_dir)
        logger.info("split %s into %d chunks of <=%d pages",
                    path, len(chunk_paths), pages_per_chunk)

        if len(chunk_paths) == 1:
            return convert_chunk(chunk_paths[0])

        # "spawn" rather than the Linux default "fork": this runs inside a
        # FastAPI threadpool worker, and forking a process that holds threads
        # can deadlock if a lock is held at fork time.
        import multiprocessing

        context = multiprocessing.get_context("spawn")
        workers = max(1, min(max_workers, len(chunk_paths)))
        with ProcessPoolExecutor(max_workers=workers, mp_context=context) as pool:
            # map() preserves input order, so pages stay sequential.
            parts = list(pool.map(convert_chunk, chunk_paths))

        return "\n\n".join(part for part in parts if part and part.strip())
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def should_chunk(path: str, min_pages: int = MIN_PAGES_FOR_PARALLEL) -> bool:
    """True if ``path`` is a PDF large enough to be worth chunking."""
    pages = page_count(path)
    return pages is not None and pages >= min_pages
