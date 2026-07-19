"""
RabbitMQ message contract and topology.

The broker itself is not under test; what is, is the message round trip and the
declaration arguments, since a wrong dead-letter setting silently sends retries
to the wrong place and only shows up under load.
"""
import pika
import pytest

import config
import taskqueue
from taskqueue import ChunkTask


def make_task(**overrides) -> ChunkTask:
    base = dict(
        job_id="job-1", chunk_index=3, doc_key="doc-1", file_hash="hash-1",
        start_page=60, end_page=80, total_chunks=5, use_llm=False,
    )
    return ChunkTask(**{**base, **overrides})


class TestSerialisation:
    def test_round_trips_every_field(self):
        task = make_task()
        assert ChunkTask.from_bytes(task.to_bytes()) == task

    def test_whole_file_sentinel_survives(self):
        """end_page == -1 means "not a page range"; losing it would make the
        worker try to page-slice a non-PDF."""
        task = make_task(end_page=-1)
        assert ChunkTask.from_bytes(task.to_bytes()).end_page == -1

    def test_use_llm_stays_boolean(self):
        task = make_task(use_llm=True)
        assert ChunkTask.from_bytes(task.to_bytes()).use_llm is True

    def test_attempt_defaults_to_one(self):
        assert make_task().attempt == 1

    def test_next_attempt_increments_and_preserves_the_rest(self):
        task = make_task(attempt=2)
        nxt = task.next_attempt()
        assert nxt.attempt == 3
        assert nxt.job_id == task.job_id
        assert nxt.chunk_index == task.chunk_index
        assert nxt.start_page == task.start_page

    def test_tasks_are_immutable(self):
        """Frozen so a handler cannot mutate a task and republish something
        subtly different from what it received."""
        import dataclasses

        import pytest

        with pytest.raises(dataclasses.FrozenInstanceError):
            make_task().chunk_index = 99


class TestRetryTiers:
    def test_each_attempt_maps_to_its_own_delay_queue(self, monkeypatch):
        monkeypatch.setattr(config, "RETRY_DELAYS", [5, 30, 300])
        assert taskqueue.retry_queue_name(1).endswith(".retry.1")
        assert taskqueue.retry_queue_name(2).endswith(".retry.2")
        assert taskqueue.retry_queue_name(3).endswith(".retry.3")

    def test_attempts_beyond_the_last_tier_clamp(self, monkeypatch):
        """MAX_ATTEMPTS could be raised past the configured tiers; that must
        reuse the longest delay, not address a queue that was never declared."""
        monkeypatch.setattr(config, "RETRY_DELAYS", [5, 30])
        assert taskqueue.retry_queue_name(7).endswith(".retry.2")


class RecordingChannel:
    """Captures declarations instead of talking to a broker."""

    def __init__(self):
        self.exchanges = {}
        self.queues = {}
        self.bindings = []

    def exchange_declare(self, exchange, kind, durable=False, **kw):
        self.exchanges[exchange] = {"type": kind, "durable": durable}

    def queue_declare(self, queue, durable=False, arguments=None, **kw):
        self.queues[queue] = {"durable": durable, "arguments": arguments or {}}

    def queue_bind(self, queue, exchange, routing_key=None, **kw):
        self.bindings.append((queue, exchange, routing_key))


class TestTopology:
    def declared(self, monkeypatch):
        monkeypatch.setattr(config, "RETRY_DELAYS", [5, 30, 300])
        ch = RecordingChannel()
        taskqueue.declare(ch)
        return ch

    def test_work_queue_is_durable(self, monkeypatch):
        """Messages outlive a broker restart, so a queued job is not lost."""
        ch = self.declared(monkeypatch)
        assert ch.queues[config.RABBITMQ_QUEUE]["durable"] is True

    def test_work_queue_dead_letters_to_the_dlx(self, monkeypatch):
        ch = self.declared(monkeypatch)
        args = ch.queues[config.RABBITMQ_QUEUE]["arguments"]
        assert args["x-dead-letter-exchange"] == taskqueue.DLX_EXCHANGE

    def test_each_retry_tier_has_its_own_ttl(self, monkeypatch):
        """Per-tier queues, not per-message TTL: RabbitMQ only expires the
        message at the head, so a 300s message would block a 5s one behind it."""
        ch = self.declared(monkeypatch)
        ttls = [
            ch.queues[f"{config.RABBITMQ_QUEUE}.retry.{tier}"]["arguments"]["x-message-ttl"]
            for tier in (1, 2, 3)
        ]
        assert ttls == [5000, 30000, 300000]

    def test_retry_queues_dead_letter_back_into_the_work_queue(self, monkeypatch):
        """This is what makes a delayed retry actually get redelivered."""
        ch = self.declared(monkeypatch)
        for tier in (1, 2, 3):
            args = ch.queues[f"{config.RABBITMQ_QUEUE}.retry.{tier}"]["arguments"]
            assert args["x-dead-letter-exchange"] == config.RABBITMQ_EXCHANGE
            assert args["x-dead-letter-routing-key"] == config.RABBITMQ_QUEUE

    def test_failed_queue_exists_and_is_bound_to_the_dlx(self, monkeypatch):
        """Poison messages are retained for inspection rather than dropped."""
        ch = self.declared(monkeypatch)
        assert taskqueue.FAILED_QUEUE in ch.queues
        assert (taskqueue.FAILED_QUEUE, taskqueue.DLX_EXCHANGE,
                config.RABBITMQ_QUEUE) in ch.bindings

    def test_retry_queues_do_not_dead_letter_to_the_failed_queue(self, monkeypatch):
        """A retry must go back to work, not straight to the graveyard — the
        easiest way to get this wrong is to reuse the DLX everywhere."""
        ch = self.declared(monkeypatch)
        for tier in (1, 2, 3):
            args = ch.queues[f"{config.RABBITMQ_QUEUE}.retry.{tier}"]["arguments"]
            assert args["x-dead-letter-exchange"] != taskqueue.DLX_EXCHANGE

    def test_declaration_is_idempotent(self, monkeypatch):
        """Every pod declares on boot."""
        ch = self.declared(monkeypatch)
        before = (len(ch.queues), len(ch.exchanges))
        taskqueue.declare(ch)
        assert (len(ch.queues), len(ch.exchanges)) == before


class PublishingChannel:
    """A channel that fails its first N publishes with a lost stream."""

    def __init__(self, failures=0):
        self.failures = failures
        self.published = []

    def basic_publish(self, exchange, routing_key, body, properties=None):
        if self.failures > 0:
            self.failures -= 1
            raise pika.exceptions.StreamLostError("Stream connection lost")
        self.published.append((exchange, routing_key, body))


class TestStaleConnection:
    """
    The producer only touches its connection when a request arrives, so an idle
    BlockingConnection misses heartbeats and the broker closes it. The next
    publish then dies mid-request and the caller sees a 400 for a file that is
    perfectly convertible — which is exactly what a long-running job between
    two submissions caused in CI.
    """

    def channels(self, monkeypatch, first_failures):
        """Hand out a broken channel first, then a healthy one."""
        made = [PublishingChannel(failures=first_failures), PublishingChannel()]
        handed = []

        def fake_channel():
            ch = made[min(len(handed), len(made) - 1)]
            handed.append(ch)
            return ch

        monkeypatch.setattr(taskqueue, "channel", fake_channel)
        monkeypatch.setattr(taskqueue, "close", lambda: handed.append(None))
        return made

    def test_publish_survives_a_dropped_connection(self, monkeypatch):
        stale, fresh = self.channels(monkeypatch, first_failures=1)
        taskqueue.publish([make_task()])
        assert stale.published == []
        assert len(fresh.published) == 1

    def test_reconnect_drops_the_stale_connection_first(self, monkeypatch):
        """Without close(), connect() returns the same dead connection and the
        retry fails identically."""
        closed = []
        monkeypatch.setattr(taskqueue, "close", lambda: closed.append(True))
        made = [PublishingChannel(failures=1), PublishingChannel()]
        monkeypatch.setattr(taskqueue, "channel", lambda: made.pop(0) if len(made) > 1 else made[0])
        taskqueue.publish([make_task()])
        assert closed == [True]

    def test_every_task_reaches_the_broker_after_a_reconnect(self, monkeypatch):
        _, fresh = self.channels(monkeypatch, first_failures=1)
        taskqueue.publish([make_task(chunk_index=i) for i in range(4)])
        assert len(fresh.published) == 4

    def test_a_second_failure_propagates(self, monkeypatch):
        """One retry, not an infinite loop: a genuinely down broker must still
        surface as an error rather than hang the request."""
        ch = PublishingChannel(failures=99)
        monkeypatch.setattr(taskqueue, "channel", lambda: ch)
        monkeypatch.setattr(taskqueue, "close", lambda: None)
        with pytest.raises(pika.exceptions.AMQPError):
            taskqueue.publish([make_task()])

    def test_retry_publish_reconnects_too(self, monkeypatch):
        _, fresh = self.channels(monkeypatch, first_failures=1)
        taskqueue.publish_retry(make_task())
        assert len(fresh.published) == 1

    def test_failed_publish_reconnects_too(self, monkeypatch):
        _, fresh = self.channels(monkeypatch, first_failures=1)
        taskqueue.publish_failed(make_task(), "boom")
        assert len(fresh.published) == 1

    def test_a_healthy_connection_is_not_reopened(self, monkeypatch):
        closed = []
        monkeypatch.setattr(taskqueue, "close", lambda: closed.append(True))
        ch = PublishingChannel()
        monkeypatch.setattr(taskqueue, "channel", lambda: ch)
        taskqueue.publish([make_task()])
        assert closed == []
        assert len(ch.published) == 1
