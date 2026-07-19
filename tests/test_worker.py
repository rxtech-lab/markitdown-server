"""
The consumer: receive a chunk -> convert -> record -> assemble.

Conversion is stubbed (markitdown is slow and covered by test_chunker); what is
under test is the orchestration around it — retries, at-least-once duplicate
delivery, and who assembles the finished document.
"""
import pytest

import config
import jobstore
import storage
import taskqueue
import worker
from taskqueue import ChunkTask


@pytest.fixture
def stub_convert(monkeypatch):
    """Replace conversion with a deterministic, instant stand-in."""
    calls = []

    def _convert(file_hash, start, end, use_llm):
        calls.append((file_hash, start, end, use_llm))
        return f"CHUNK[{start}:{end}]"

    monkeypatch.setattr(worker, "convert_range", _convert)
    return calls


def make_job(broker, chunks=3, use_llm=False):
    """Create a job and publish its chunks, as the producer does."""
    job_id = jobstore.new_job_id()
    doc_key = f"doc-{job_id}"
    ranges = [(i * 10, (i + 1) * 10) for i in range(chunks)]
    jobstore.create_job(job_id, doc_key, "filehash", "http://x/f.pdf",
                        ranges, use_llm)
    taskqueue.publish([
        ChunkTask(job_id=job_id, chunk_index=i, doc_key=doc_key,
                  file_hash="filehash", start_page=s, end_page=e,
                  total_chunks=chunks, use_llm=use_llm)
        for i, (s, e) in enumerate(ranges)
    ])
    return job_id


class TestHappyPath:
    def test_job_completes_and_publishes_a_readable_document(
        self, database, s3, broker, fast_queue, stub_convert
    ):
        job_id = make_job(broker, 3)
        broker.drain(worker.handle)

        job = jobstore.get_job(job_id)
        assert job["status"] == "done"
        assert job["remaining_chunks"] == 0
        assert storage.get_page(job["doc_key"], 1) is not None

    def test_chunks_are_assembled_in_page_order(
        self, database, s3, broker, fast_queue, stub_convert
    ):
        job_id = make_job(broker, 5)
        broker.drain(worker.handle)

        doc_key = jobstore.get_job(job_id)["doc_key"]
        manifest = storage.get_manifest(doc_key)
        text = "".join(
            storage.get_page(doc_key, n)
            for n in range(1, manifest["total_pages"] + 1)
        )
        positions = [text.index(f"CHUNK[{i * 10}:{(i + 1) * 10}]") for i in range(5)]
        assert positions == sorted(positions)

    def test_out_of_order_delivery_still_assembles_in_page_order(
        self, database, s3, broker, fast_queue, stub_convert
    ):
        """
        RabbitMQ gives no cross-consumer ordering guarantee, and a retry pushes
        its chunk arbitrarily late. Assembly must therefore sort by chunk
        index, not by completion order.
        """
        job_id = make_job(broker, 5)
        broker.ready.reverse()          # deliver chunk 4 first, chunk 0 last
        broker.drain(worker.handle)

        doc_key = jobstore.get_job(job_id)["doc_key"]
        manifest = storage.get_manifest(doc_key)
        text = "".join(
            storage.get_page(doc_key, n)
            for n in range(1, manifest["total_pages"] + 1)
        )
        positions = [text.index(f"CHUNK[{i * 10}:{(i + 1) * 10}]") for i in range(5)]
        assert positions == sorted(positions)

    def test_use_llm_flag_reaches_the_converter(
        self, database, s3, broker, fast_queue, stub_convert
    ):
        make_job(broker, 1, use_llm=True)
        broker.drain(worker.handle)
        assert stub_convert[0][3] is True

    def test_document_is_recorded_for_the_cache(
        self, database, s3, broker, fast_queue, stub_convert
    ):
        job_id = make_job(broker, 2)
        broker.drain(worker.handle)

        cached = jobstore.find_cached_document(jobstore.get_job(job_id)["doc_key"])
        assert cached is not None
        assert cached["total_chunks"] == 2


class TestRetries:
    def test_transient_failure_is_republished_and_succeeds(
        self, database, s3, broker, fast_queue, monkeypatch
    ):
        monkeypatch.setattr(config, "MAX_ATTEMPTS", 3)
        attempts = []

        def flaky(file_hash, start, end, use_llm):
            attempts.append(start)
            if len(attempts) == 1:
                raise RuntimeError("transient parser hiccup")
            return "recovered"

        monkeypatch.setattr(worker, "convert_range", flaky)

        job_id = make_job(broker, 1)
        broker.drain(worker.handle)

        assert len(attempts) == 2
        assert len(broker.retried) == 1
        assert broker.retried[0].attempt == 2
        assert jobstore.get_job(job_id)["status"] == "done"

    def test_retry_carries_an_incrementing_attempt(
        self, database, s3, broker, fast_queue, monkeypatch
    ):
        monkeypatch.setattr(config, "MAX_ATTEMPTS", 3)
        monkeypatch.setattr(
            worker, "convert_range",
            lambda *a: (_ for _ in ()).throw(RuntimeError("always fails")),
        )
        make_job(broker, 1)
        broker.drain(worker.handle)

        assert [t.attempt for t in broker.retried] == [2, 3]

    def test_poison_chunk_goes_to_the_failed_queue_and_fails_the_job(
        self, database, s3, broker, fast_queue, monkeypatch
    ):
        """A document silently missing pages is worse than an error."""
        monkeypatch.setattr(config, "MAX_ATTEMPTS", 2)

        def selective(file_hash, start, end, use_llm):
            if start == 10:
                raise RuntimeError("corrupt page range")
            return "fine"

        monkeypatch.setattr(worker, "convert_range", selective)

        job_id = make_job(broker, 3)
        broker.drain(worker.handle)

        job = jobstore.get_job(job_id)
        assert job["status"] == "failed"
        assert job["error_chunk"] == 1
        assert len(broker.failed) == 1
        assert "corrupt page range" in broker.failed[0][1]

    def test_no_document_is_published_for_a_failed_job(
        self, database, s3, broker, fast_queue, monkeypatch
    ):
        monkeypatch.setattr(config, "MAX_ATTEMPTS", 1)
        monkeypatch.setattr(
            worker, "convert_range",
            lambda *a: (_ for _ in ()).throw(RuntimeError("nope")),
        )
        job_id = make_job(broker, 2)
        broker.drain(worker.handle)

        doc_key = jobstore.get_job(job_id)["doc_key"]
        assert jobstore.get_job(job_id)["status"] == "failed"
        assert storage.get_manifest(doc_key) is None
        assert jobstore.find_cached_document(doc_key) is None

    def test_chunks_of_an_already_failed_job_are_dropped(
        self, database, s3, broker, fast_queue, stub_convert
    ):
        job_id = make_job(broker, 3)
        jobstore.fail_job(job_id, "failed elsewhere")
        broker.drain(worker.handle)

        assert jobstore.get_job(job_id)["status"] == "failed"
        assert stub_convert == []      # no work done on a dead job

    def test_assembly_failure_fails_the_job(
        self, database, s3, broker, fast_queue, stub_convert, monkeypatch
    ):
        job_id = make_job(broker, 1)
        monkeypatch.setattr(storage, "get_part", lambda *a: None)
        broker.drain(worker.handle)
        assert jobstore.get_job(job_id)["status"] == "failed"


class TestAtLeastOnceDelivery:
    def test_duplicate_delivery_does_not_corrupt_the_counter(
        self, database, s3, broker, fast_queue, stub_convert
    ):
        """
        RabbitMQ redelivers a message whose consumer died before acking, so a
        chunk can genuinely be converted twice. The second completion must be a
        no-op — otherwise remaining_chunks goes negative and nobody ever sees 0
        to assemble the document.
        """
        job_id = make_job(broker, 2)
        first = broker.ready[0]

        worker.handle(first)
        worker.handle(first)          # redelivery of the same message

        job = jobstore.get_job(job_id)
        assert job["remaining_chunks"] == 1
        assert job["status"] != "failed"

    def test_redelivery_after_completion_never_double_assembles(
        self, database, s3, broker, fast_queue, stub_convert
    ):
        job_id = make_job(broker, 2)
        tasks = list(broker.ready)
        for task in tasks:
            worker.handle(task)
        assert jobstore.get_job(job_id)["status"] == "done"

        # Every message redelivered after the job finished.
        for task in tasks:
            worker.handle(task)
        assert jobstore.get_job(job_id)["status"] == "done"
        assert jobstore.get_job(job_id)["remaining_chunks"] == 0


class TestSourceCache:
    def test_source_is_fetched_once_for_repeated_chunks(
        self, database, s3, fast_queue, tmp_path, monkeypatch
    ):
        """A worker receiving several chunks of one document downloads once."""
        monkeypatch.setattr(config, "SOURCE_CACHE_DIR", str(tmp_path / "cache"))
        src = tmp_path / "src.bin"
        src.write_bytes(b"the source document")
        storage.put_source("filehash", str(src))

        downloads = []
        original = storage.get_source

        def counting(file_hash, dest):
            downloads.append(file_hash)
            return original(file_hash, dest)

        monkeypatch.setattr(storage, "get_source", counting)
        for _ in range(4):
            worker.fetch_source("filehash")

        assert len(downloads) == 1

    def test_prune_evicts_until_within_budget(self, fast_queue, tmp_path, monkeypatch):
        cache = tmp_path / "cache"
        cache.mkdir()
        monkeypatch.setattr(config, "SOURCE_CACHE_DIR", str(cache))
        for name in ("a", "b", "c"):
            (cache / name).write_bytes(b"x" * 1000)

        worker.prune_source_cache(max_bytes=1500)
        assert len(list(cache.iterdir())) <= 2

    def test_failed_download_leaves_no_partial_cache_entry(
        self, database, s3, fast_queue, tmp_path, monkeypatch
    ):
        """A truncated download must not later look like a valid cache hit."""
        monkeypatch.setattr(config, "SOURCE_CACHE_DIR", str(tmp_path / "cache"))
        monkeypatch.setattr(
            storage, "get_source",
            lambda *a: (_ for _ in ()).throw(RuntimeError("network died")),
        )

        with pytest.raises(RuntimeError):
            worker.fetch_source("filehash")

        cache_dir = tmp_path / "cache"
        assert not (cache_dir / "filehash").exists()
        assert list(cache_dir.iterdir()) == []
