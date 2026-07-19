import pytest
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas


def write_pdf(path: str, pages: int, lines_per_page: int = 20) -> str:
    """
    Write a prose-style PDF where every page carries a unique marker.

    The markers let tests assert on both content preservation and page ordering
    after a document has been split and reassembled.
    """
    c = canvas.Canvas(path, pagesize=letter)
    for page in range(1, pages + 1):
        text = c.beginText(72, 720)
        text.textLine(f"PAGEMARKER{page:05d}")
        for line in range(lines_per_page):
            text.textLine(
                f"Lorem ipsum dolor sit amet consectetur adipiscing elit, "
                f"line {line} of page {page}."
            )
        c.drawText(text)
        c.showPage()
    c.save()
    return path


@pytest.fixture
def make_pdf(tmp_path):
    """Factory fixture: make_pdf(pages) -> path to a generated PDF."""
    def _make(pages: int, name: str = "doc.pdf", lines_per_page: int = 20) -> str:
        return write_pdf(str(tmp_path / name), pages, lines_per_page)

    return _make


@pytest.fixture
def small_pdf(make_pdf):
    return make_pdf(3)


@pytest.fixture
def multi_chunk_pdf(make_pdf):
    """25 pages — splits into multiple chunks at the default 20/chunk."""
    return make_pdf(25)
