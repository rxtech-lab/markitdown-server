"""
Chunk planning and page-range extraction.

plan_chunks() is pure, so the partitioning rules — which used to be entangled
with writing files to a temp dir — can be asserted directly.
"""
import pytest

from chunker import WHOLE_FILE, extract_pages, page_count, plan_chunks


class TestPlanChunks:
    def test_non_pdf_is_a_single_whole_file_chunk(self):
        assert plan_chunks(None, 20, 40) == [WHOLE_FILE]

    def test_small_pdf_is_not_split(self):
        assert plan_chunks(39, 20, 40) == [WHOLE_FILE]

    def test_at_threshold_is_split(self):
        assert plan_chunks(40, 20, 40) == [(0, 20), (20, 40)]

    def test_exact_multiple_has_no_empty_trailing_chunk(self):
        assert plan_chunks(60, 20, 40) == [(0, 20), (20, 40), (40, 60)]

    def test_remainder_becomes_a_short_final_chunk(self):
        assert plan_chunks(45, 20, 40) == [(0, 20), (20, 40), (40, 45)]

    def test_ranges_are_contiguous_and_cover_every_page(self):
        ranges = plan_chunks(137, 20, 40)
        assert ranges[0][0] == 0
        assert ranges[-1][1] == 137
        for (_, end), (start, _) in zip(ranges, ranges[1:]):
            assert end == start
        assert sum(end - start for start, end in ranges) == 137

    def test_chunk_size_of_one(self):
        assert plan_chunks(3, 1, 1) == [(0, 1), (1, 2), (2, 3)]

    def test_zero_pages(self):
        # A zero-page PDF is below any threshold, so it stays whole-file and
        # the converter deals with whatever it is.
        assert plan_chunks(0, 20, 40) == [WHOLE_FILE]

    def test_invalid_chunk_size_rejected(self):
        with pytest.raises(ValueError):
            plan_chunks(100, 0, 40)


class TestPageCount:
    def test_counts_pages(self, make_pdf):
        assert page_count(make_pdf(7)) == 7

    def test_non_pdf_returns_none(self, tmp_path):
        path = tmp_path / "notes.txt"
        path.write_text("this is not a pdf")
        assert page_count(str(path)) is None

    def test_missing_file_returns_none(self):
        assert page_count("/nonexistent/file.pdf") is None


class TestExtractPages:
    def test_extracts_the_requested_range(self, make_pdf, tmp_path):
        src = make_pdf(25)
        out = str(tmp_path / "range.pdf")
        extract_pages(src, 5, 15, out)
        assert page_count(out) == 10

    def test_final_short_range(self, make_pdf, tmp_path):
        src = make_pdf(25)
        out = str(tmp_path / "range.pdf")
        extract_pages(src, 20, 25, out)
        assert page_count(out) == 5

    def test_whole_file_sentinel_returns_the_source_untouched(self, make_pdf):
        src = make_pdf(3)
        assert extract_pages(src, 0, -1, "/tmp/unused.pdf") == src

    def test_extracted_range_holds_exactly_its_own_pages(self, make_pdf, tmp_path):
        """The marker on each page proves ranges neither drop nor overlap."""
        from markitdown import MarkItDown

        src = make_pdf(10)
        out = str(tmp_path / "range.pdf")
        extract_pages(src, 3, 6, out)

        markdown = MarkItDown().convert(out).markdown
        # Pages are 0-indexed in the range, 1-indexed in the marker.
        assert "PAGEMARKER00004" in markdown
        assert "PAGEMARKER00005" in markdown
        assert "PAGEMARKER00006" in markdown
        assert "PAGEMARKER00003" not in markdown
        assert "PAGEMARKER00007" not in markdown

    def test_full_split_reassembles_in_order(self, make_pdf, tmp_path):
        """Every page survives a plan/extract round trip, in the right order."""
        from markitdown import MarkItDown

        src = make_pdf(25)
        seen = []
        for index, (start, end) in enumerate(plan_chunks(25, 10, 1)):
            out = str(tmp_path / f"chunk_{index}.pdf")
            extract_pages(src, start, end, out)
            seen.append(MarkItDown().convert(out).markdown)

        combined = "\n\n".join(seen)
        positions = [combined.index(f"PAGEMARKER{n:05d}") for n in range(1, 26)]
        assert positions == sorted(positions)
