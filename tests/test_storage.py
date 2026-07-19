"""
Content-addressed storage: key derivation and object round trips.

The key derivation tests are the interesting ones — the cache is only safe if
the key changes whenever anything that affects the output changes.
"""
import config
import storage


class TestKeyDerivation:
    def test_same_bytes_hash_the_same(self, tmp_path):
        a, b = tmp_path / "a.bin", tmp_path / "b.bin"
        a.write_bytes(b"identical content")
        b.write_bytes(b"identical content")
        assert storage.hash_file(str(a)) == storage.hash_file(str(b))

    def test_different_bytes_hash_differently(self, tmp_path):
        a, b = tmp_path / "a.bin", tmp_path / "b.bin"
        a.write_bytes(b"one")
        b.write_bytes(b"two")
        assert storage.hash_file(str(a)) != storage.hash_file(str(b))

    def test_llm_flag_changes_the_doc_key(self):
        """LLM and non-LLM output differ, so they must not share a cache
        entry."""
        assert storage.make_doc_key("h", True) != storage.make_doc_key("h", False)

    def test_page_size_changes_the_doc_key(self, monkeypatch):
        before = storage.make_doc_key("h", False)
        monkeypatch.setattr(config, "PAGE_SIZE", config.PAGE_SIZE + 1)
        assert storage.make_doc_key("h", False) != before

    def test_chunk_size_changes_the_doc_key(self, monkeypatch):
        """Chunk boundaries affect the assembled output, so they belong in the
        fingerprint."""
        before = storage.make_doc_key("h", False)
        monkeypatch.setattr(config, "PAGES_PER_CHUNK", config.PAGES_PER_CHUNK + 1)
        assert storage.make_doc_key("h", False) != before

    def test_doc_key_is_stable_across_calls(self):
        assert storage.make_doc_key("h", False) == storage.make_doc_key("h", False)

    def test_part_keys_sort_in_page_order(self):
        """Zero-padded so lexicographic listing matches page order."""
        keys = [storage.part_key("d", i) for i in (2, 10, 1)]
        assert sorted(keys) == [
            storage.part_key("d", 1),
            storage.part_key("d", 2),
            storage.part_key("d", 10),
        ]


class TestObjects:
    def test_source_round_trip(self, s3, tmp_path):
        src = tmp_path / "in.pdf"
        src.write_bytes(b"pdf bytes here")
        storage.put_source("hash1", str(src))

        dest = tmp_path / "out.pdf"
        storage.get_source("hash1", str(dest))
        assert dest.read_bytes() == b"pdf bytes here"

    def test_put_source_skips_an_existing_object(self, s3, tmp_path):
        src = tmp_path / "in.pdf"
        src.write_bytes(b"original")
        storage.put_source("hash1", str(src))

        # Same key, different bytes: content addressing means this cannot
        # legitimately happen, so the upload should be skipped entirely.
        src.write_bytes(b"different")
        storage.put_source("hash1", str(src))

        dest = tmp_path / "out.pdf"
        storage.get_source("hash1", str(dest))
        assert dest.read_bytes() == b"original"

    def test_part_round_trip(self, s3):
        storage.put_part("doc1", 3, "# chunk three")
        assert storage.get_part("doc1", 3) == "# chunk three"

    def test_missing_part_is_none(self, s3):
        assert storage.get_part("doc1", 99) is None

    def test_document_round_trip(self, s3):
        pages = ["page one", "page two", "page three"]
        manifest = storage.put_document("doc1", pages, 42)

        assert manifest["total_pages"] == 3
        assert manifest["total_length"] == 42
        assert storage.get_manifest("doc1") == manifest
        assert storage.get_page("doc1", 1) == "page one"
        assert storage.get_page("doc1", 3) == "page three"

    def test_pages_are_one_indexed(self, s3):
        storage.put_document("doc1", ["only page"], 9)
        assert storage.get_page("doc1", 0) is None
        assert storage.get_page("doc1", 1) == "only page"

    def test_missing_manifest_is_none(self, s3):
        assert storage.get_manifest("nope") is None

    def test_unicode_survives_the_round_trip(self, s3):
        storage.put_document("doc1", ["héllo — wörld 🎉"], 15)
        assert storage.get_page("doc1", 1) == "héllo — wörld 🎉"

    def test_health_reports_true_for_a_live_bucket(self, s3):
        assert storage.health() is True
