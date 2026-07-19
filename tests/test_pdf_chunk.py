"""
Unit tests for page-chunked PDF conversion.

These run against real generated PDFs rather than mocks — the whole point of
the module is how pypdf/pdfplumber/pdfminer behave on actual documents, and a
mocked parser would test nothing.
"""
import os

import pytest

import pdf_chunk


class TestPageCount:
    def test_counts_pages(self, make_pdf):
        assert pdf_chunk.page_count(make_pdf(7)) == 7

    def test_returns_none_for_non_pdf(self, tmp_path):
        path = tmp_path / "not.pdf"
        path.write_text("this is plainly not a pdf")
        assert pdf_chunk.page_count(str(path)) is None

    def test_returns_none_for_missing_file(self, tmp_path):
        assert pdf_chunk.page_count(str(tmp_path / "nope.pdf")) is None


class TestShouldChunk:
    def test_true_at_or_above_threshold(self, make_pdf):
        assert pdf_chunk.should_chunk(make_pdf(10), min_pages=10) is True

    def test_false_below_threshold(self, make_pdf):
        assert pdf_chunk.should_chunk(make_pdf(9), min_pages=10) is False

    def test_false_for_non_pdf(self, tmp_path):
        path = tmp_path / "not.pdf"
        path.write_text("nope")
        assert pdf_chunk.should_chunk(str(path), min_pages=1) is False


class TestSplitPdf:
    def test_splits_evenly(self, make_pdf, tmp_path):
        out = tmp_path / "chunks"
        out.mkdir()
        chunks = pdf_chunk.split_pdf(make_pdf(20), 5, str(out))
        assert len(chunks) == 4
        assert all(pdf_chunk.page_count(c) == 5 for c in chunks)

    def test_last_chunk_holds_remainder(self, make_pdf, tmp_path):
        out = tmp_path / "chunks"
        out.mkdir()
        chunks = pdf_chunk.split_pdf(make_pdf(22), 5, str(out))
        assert len(chunks) == 5
        assert [pdf_chunk.page_count(c) for c in chunks] == [5, 5, 5, 5, 2]

    def test_single_chunk_when_smaller_than_chunk_size(self, make_pdf, tmp_path):
        out = tmp_path / "chunks"
        out.mkdir()
        chunks = pdf_chunk.split_pdf(make_pdf(3), 20, str(out))
        assert len(chunks) == 1
        assert pdf_chunk.page_count(chunks[0]) == 3

    def test_preserves_total_page_count(self, make_pdf, tmp_path):
        out = tmp_path / "chunks"
        out.mkdir()
        chunks = pdf_chunk.split_pdf(make_pdf(37), 8, str(out))
        assert sum(pdf_chunk.page_count(c) for c in chunks) == 37

    def test_chunks_are_ordered_by_name(self, make_pdf, tmp_path):
        out = tmp_path / "chunks"
        out.mkdir()
        chunks = pdf_chunk.split_pdf(make_pdf(30), 10, str(out))
        assert chunks == sorted(chunks)

    def test_rejects_zero_pages_per_chunk(self, make_pdf, tmp_path):
        out = tmp_path / "chunks"
        out.mkdir()
        with pytest.raises(ValueError):
            pdf_chunk.split_pdf(make_pdf(5), 0, str(out))


class TestConvertChunk:
    def test_extracts_text_from_chunk(self, small_pdf):
        markdown = pdf_chunk.convert_chunk(small_pdf)
        assert "PAGEMARKER00001" in markdown
        assert "PAGEMARKER00003" in markdown


class TestConvertPdfChunked:
    def test_single_chunk_document(self, make_pdf):
        markdown = pdf_chunk.convert_pdf_chunked(
            make_pdf(5), pages_per_chunk=20, max_workers=2
        )
        assert "PAGEMARKER00001" in markdown
        assert "PAGEMARKER00005" in markdown

    def test_retains_every_page_across_chunks(self, multi_chunk_pdf):
        markdown = pdf_chunk.convert_pdf_chunked(
            multi_chunk_pdf, pages_per_chunk=10, max_workers=2
        )
        for page in range(1, 26):
            assert f"PAGEMARKER{page:05d}" in markdown, f"page {page} missing"

    def test_preserves_page_order(self, multi_chunk_pdf):
        markdown = pdf_chunk.convert_pdf_chunked(
            multi_chunk_pdf, pages_per_chunk=10, max_workers=2
        )
        positions = [
            markdown.index(f"PAGEMARKER{page:05d}") for page in range(1, 26)
        ]
        assert positions == sorted(positions), "pages came back out of order"

    def test_chunk_size_does_not_change_content(self, make_pdf):
        """Page count per chunk is a tuning knob, not a semantic one."""
        path = make_pdf(24)
        a = pdf_chunk.convert_pdf_chunked(path, pages_per_chunk=6, max_workers=2)
        b = pdf_chunk.convert_pdf_chunked(path, pages_per_chunk=12, max_workers=2)
        for page in range(1, 25):
            marker = f"PAGEMARKER{page:05d}"
            assert marker in a and marker in b

    def test_matches_sequential_conversion(self, make_pdf):
        """
        Chunked output must carry the same page content as the whole-document
        path. Exact string equality is not asserted: markitdown picks its
        extraction strategy per document, so deciding it per chunk can shift
        whitespace at chunk boundaries.
        """
        from markitdown import MarkItDown

        path = make_pdf(24)
        sequential = MarkItDown().convert(path).text_content
        chunked = pdf_chunk.convert_pdf_chunked(
            path, pages_per_chunk=8, max_workers=2
        )

        for page in range(1, 25):
            marker = f"PAGEMARKER{page:05d}"
            assert marker in sequential and marker in chunked

        # Allow small boundary differences but catch wholesale content loss.
        ratio = len(chunked) / len(sequential)
        assert 0.95 < ratio < 1.05, f"length drifted: {ratio:.3f}"

    def test_cleans_up_temp_chunks(self, multi_chunk_pdf, tmp_path):
        before = set(os.listdir("/tmp")) if os.path.isdir("/tmp") else set()
        pdf_chunk.convert_pdf_chunked(
            multi_chunk_pdf, pages_per_chunk=10, max_workers=2
        )
        after = set(os.listdir("/tmp")) if os.path.isdir("/tmp") else set()
        leaked = {d for d in after - before if d.startswith("pdf_chunks_")}
        assert not leaked, f"left temp dirs behind: {leaked}"

    def test_propagates_worker_failure(self, tmp_path):
        bad = tmp_path / "bad.pdf"
        bad.write_text("not a pdf at all")
        with pytest.raises(Exception):
            pdf_chunk.convert_pdf_chunked(str(bad), pages_per_chunk=5)
