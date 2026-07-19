"""
Job and document state.

The completion counter is the piece that matters most: RabbitMQ distributes the
work, but it cannot answer "did I just finish the last of 25 chunks?" — and that
answer decides who assembles the document, exactly once.
"""
import threading

import jobstore
from jobstore import CHUNK_ALREADY_DONE, JOB_ALREADY_TERMINAL


def make_job(chunks: int = 3, use_llm: bool = False) -> str:
    job_id = jobstore.new_job_id()
    ranges = [(i * 10, (i + 1) * 10) for i in range(chunks)]
    jobstore.create_job(job_id, f"key-{job_id}", "hash", "http://x/f.pdf",
                        ranges, use_llm)
    return job_id


class TestCreateJob:
    def test_creates_job_and_chunk_rows(self, database):
        job_id = make_job(4)
        job = jobstore.get_job(job_id)
        assert job["status"] == "queued"
        assert job["total_chunks"] == 4
        assert job["remaining_chunks"] == 4
        assert jobstore.chunk_indices(job_id) == [0, 1, 2, 3]

    def test_progress_starts_at_zero(self, database):
        job_id = make_job(4)
        assert jobstore.job_progress(job_id) == {
            "completed": 0, "total": 4, "percent": 0
        }


class TestCompleteChunk:
    def test_counts_down_to_zero_for_the_assembler(self, database):
        job_id = make_job(3)
        results = [jobstore.complete_chunk(job_id, i, "k") for i in range(3)]
        assert results == [2, 1, 0]

    def test_duplicate_completion_does_not_double_decrement(self, database):
        """
        RabbitMQ delivers at least once, so a chunk whose worker died before
        acking gets redelivered and converted twice. The second recording must
        be a no-op, or the counter goes negative and nobody ever assembles.
        """
        job_id = make_job(3)
        assert jobstore.complete_chunk(job_id, 0, "k") == 2
        assert jobstore.complete_chunk(job_id, 0, "k") == CHUNK_ALREADY_DONE
        assert jobstore.get_job(job_id)["remaining_chunks"] == 2

    def test_completion_on_a_failed_job_is_rejected(self, database):
        job_id = make_job(2)
        jobstore.fail_job(job_id, "poisoned")
        assert jobstore.complete_chunk(job_id, 0, "k") == JOB_ALREADY_TERMINAL

    def test_progress_tracks_completions(self, database):
        job_id = make_job(4)
        jobstore.complete_chunk(job_id, 0, "k")
        assert jobstore.job_progress(job_id) == {
            "completed": 1, "total": 4, "percent": 25
        }

    def test_concurrent_completion_yields_exactly_one_assembler(self, database):
        """Exactly one worker must see remaining == 0, or a document is
        assembled twice or never."""
        job_id = make_job(16)
        results: list[int] = []
        lock = threading.Lock()
        barrier = threading.Barrier(16)

        def complete(index):
            barrier.wait()
            value = jobstore.complete_chunk(job_id, index, "k")
            with lock:
                results.append(value)

        threads = [threading.Thread(target=complete, args=(i,)) for i in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        assert results.count(0) == 1
        assert sorted(results) == list(range(16))


class TestFailure:
    def test_fail_job_is_terminal(self, database):
        job_id = make_job(2)
        jobstore.fail_job(job_id, "first", 0)
        jobstore.fail_job(job_id, "second", 1)
        assert jobstore.get_job(job_id)["error"] == "first"

    def test_finish_job_blocks_later_failure(self, database):
        job_id = make_job(1)
        jobstore.finish_job(job_id)
        jobstore.fail_job(job_id, "too late")
        assert jobstore.get_job(job_id)["status"] == "done"

    def test_fail_chunk_records_the_error(self, database):
        job_id = make_job(2)
        jobstore.fail_chunk(job_id, 1, "corrupt page range")
        row = database.query_one(
            "SELECT status, last_error FROM job_chunks WHERE job_id = ? AND chunk_index = 1",
            (job_id,),
        )
        assert row["status"] == "failed"
        assert row["last_error"] == "corrupt page range"

    def test_reschedule_leaves_a_done_chunk_alone(self, database):
        """A late retry must not undo a chunk that already succeeded."""
        job_id = make_job(2)
        jobstore.complete_chunk(job_id, 0, "k")
        jobstore.reschedule_chunk(job_id, 0, "stale failure")
        row = database.query_one(
            "SELECT status FROM job_chunks WHERE job_id = ? AND chunk_index = 0",
            (job_id,),
        )
        assert row["status"] == "done"


class TestChunkState:
    def test_start_chunk_marks_running_and_flips_the_job(self, database):
        job_id = make_job(2)
        jobstore.start_chunk(job_id, 0, "worker-1", attempt=1)
        assert jobstore.get_job(job_id)["status"] == "running"
        row = database.query_one(
            "SELECT status, lease_owner, attempts FROM job_chunks "
            "WHERE job_id = ? AND chunk_index = 0",
            (job_id,),
        )
        assert row["status"] == "running"
        assert row["lease_owner"] == "worker-1"
        assert row["attempts"] == 1

    def test_start_chunk_does_not_reopen_a_done_chunk(self, database):
        """Redelivery of an already-converted chunk must not un-finish it."""
        job_id = make_job(2)
        jobstore.complete_chunk(job_id, 0, "k")
        jobstore.start_chunk(job_id, 0, "worker-2", attempt=2)
        row = database.query_one(
            "SELECT status FROM job_chunks WHERE job_id = ? AND chunk_index = 0",
            (job_id,),
        )
        assert row["status"] == "done"


class TestDocuments:
    def test_round_trips_a_document(self, database):
        jobstore.record_document("k1", "hash", 5, 2, 9000, 5000)
        doc = jobstore.find_cached_document("k1")
        assert doc["total_pages"] == 5
        assert doc["total_chunks"] == 2
        assert doc["expires_at"] > doc["created_at"]

    def test_missing_document_is_none(self, database):
        assert jobstore.find_cached_document("nope") is None

    def test_recording_twice_is_harmless(self, database):
        """Two jobs for identical content can race; both produce the same
        output, so last writer wins."""
        jobstore.record_document("k1", "hash", 5, 2, 9000, 5000)
        jobstore.record_document("k1", "hash", 5, 2, 9000, 5000)
        assert jobstore.find_cached_document("k1")["total_pages"] == 5
