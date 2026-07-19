"""
RabbitMQ job distribution.

The producer publishes one message per chunk; workers consume them with a
prefetch of 1, so the broker hands each chunk to whichever worker is free.
Adding a worker pod adds capacity with no coordination — that is what RabbitMQ
buys over a database-polled queue.

Division of labour with the database:

* **RabbitMQ owns delivery** — dispatch, redelivery when a worker dies without
  acking, retry scheduling, and dead-lettering.
* **Turso owns state** — the per-job remaining-chunk counter, progress, and the
  document manifest. A broker cannot answer "did I just finish the last of 25
  chunks?" atomically, and that question decides who assembles the document.

Delivery is at-least-once: a worker that converts a chunk and then dies before
acking will have that chunk redelivered. jobstore.complete_chunk is written to
be idempotent for exactly this reason, and storage keys are content-addressed
so a duplicate conversion rewrites identical bytes.

Topology::

    markitdown            (direct)  -> markitdown.chunks
    markitdown.retry      (direct)  -> markitdown.retry.{1,2,3}
                                       each with a TTL, dead-lettering back
                                       into markitdown.chunks
    markitdown.dlx        (direct)  -> markitdown.chunks.failed
"""
import json
import logging
import threading
from dataclasses import asdict, dataclass
from typing import Callable, Optional

import pika

import config

logger = logging.getLogger(__name__)

RETRY_EXCHANGE = f"{config.RABBITMQ_EXCHANGE}.retry"
DLX_EXCHANGE = f"{config.RABBITMQ_EXCHANGE}.dlx"
FAILED_QUEUE = f"{config.RABBITMQ_QUEUE}.failed"

_local = threading.local()


@dataclass(frozen=True)
class ChunkTask:
    """One unit of work: a page range of one document."""

    job_id: str
    chunk_index: int
    doc_key: str
    file_hash: str
    start_page: int
    end_page: int          # exclusive; -1 means the whole file
    total_chunks: int
    use_llm: bool
    attempt: int = 1

    def to_bytes(self) -> bytes:
        return json.dumps(asdict(self)).encode()

    @classmethod
    def from_bytes(cls, raw: bytes) -> "ChunkTask":
        return cls(**json.loads(raw))

    def next_attempt(self) -> "ChunkTask":
        return ChunkTask(**{**asdict(self), "attempt": self.attempt + 1})


def retry_queue_name(attempt: int) -> str:
    """Retry queue for a given attempt, clamped to the last tier."""
    tier = min(attempt, len(config.RETRY_DELAYS))
    return f"{config.RABBITMQ_QUEUE}.retry.{tier}"


def connect() -> "pika.BlockingConnection":
    """Open a connection for this thread, declaring the topology once."""
    connection = getattr(_local, "connection", None)
    if connection is not None and connection.is_open:
        return connection

    params = pika.URLParameters(config.RABBITMQ_URL)
    params.heartbeat = config.RABBITMQ_HEARTBEAT
    # Without this, a broker that is up but refusing connections (still booting)
    # fails the whole process instead of settling.
    params.blocked_connection_timeout = 30
    connection = pika.BlockingConnection(params)
    _local.connection = connection
    _local.channel = connection.channel()
    declare(_local.channel)
    return connection


def channel() -> "pika.adapters.blocking_connection.BlockingChannel":
    connect()
    return _local.channel


def close() -> None:
    connection = getattr(_local, "connection", None)
    if connection is not None:
        try:
            if connection.is_open:
                connection.close()
        except Exception:
            logger.debug("error closing rabbitmq connection", exc_info=True)
    _local.connection = None
    _local.channel = None


def declare(ch) -> None:
    """
    Declare exchanges and queues. Idempotent, so every process does it on boot.

    Everything is durable and messages are published persistent, so a broker
    restart does not lose queued work.
    """
    ch.exchange_declare(config.RABBITMQ_EXCHANGE, "direct", durable=True)
    ch.exchange_declare(RETRY_EXCHANGE, "direct", durable=True)
    ch.exchange_declare(DLX_EXCHANGE, "direct", durable=True)

    # Main work queue. Rejected messages land in the DLX.
    ch.queue_declare(
        config.RABBITMQ_QUEUE,
        durable=True,
        arguments={"x-dead-letter-exchange": DLX_EXCHANGE},
    )
    ch.queue_bind(config.RABBITMQ_QUEUE, config.RABBITMQ_EXCHANGE,
                  routing_key=config.RABBITMQ_QUEUE)

    # One retry queue per delay tier. A message sits here for the tier's TTL,
    # then dead-letters back into the main queue for another attempt.
    for tier, delay in enumerate(config.RETRY_DELAYS, start=1):
        name = f"{config.RABBITMQ_QUEUE}.retry.{tier}"
        ch.queue_declare(
            name,
            durable=True,
            arguments={
                "x-message-ttl": delay * 1000,
                "x-dead-letter-exchange": config.RABBITMQ_EXCHANGE,
                "x-dead-letter-routing-key": config.RABBITMQ_QUEUE,
            },
        )
        ch.queue_bind(name, RETRY_EXCHANGE, routing_key=name)

    # Poison messages. Retained rather than dropped so a failure can be read
    # back after the fact.
    ch.queue_declare(FAILED_QUEUE, durable=True)
    ch.queue_bind(FAILED_QUEUE, DLX_EXCHANGE, routing_key=config.RABBITMQ_QUEUE)


_PERSISTENT = pika.BasicProperties(
    delivery_mode=pika.DeliveryMode.Persistent,
    content_type="application/json",
)


def _publish(exchange: str, routing_key: str, bodies: list[bytes]) -> None:
    """
    Publish bodies, reconnecting once if the cached connection has gone stale.

    The producer only touches its connection when a request arrives, so a
    BlockingConnection sitting idle between submissions never gets to send a
    heartbeat and the broker eventually closes it. ``is_open`` still reports
    True until the socket error surfaces, so the staleness can only be
    discovered by publishing — hence retry rather than a pre-flight check.
    """
    try:
        ch = channel()
        for body in bodies:
            ch.basic_publish(exchange=exchange, routing_key=routing_key,
                             body=body, properties=_PERSISTENT)
        return
    except pika.exceptions.AMQPError:
        logger.warning("publish failed, reconnecting to rabbitmq", exc_info=True)

    close()
    ch = channel()
    for body in bodies:
        ch.basic_publish(exchange=exchange, routing_key=routing_key,
                         body=body, properties=_PERSISTENT)


def publish(tasks: list[ChunkTask]) -> None:
    """Publish chunk tasks onto the work queue."""
    _publish(config.RABBITMQ_EXCHANGE, config.RABBITMQ_QUEUE,
             [task.to_bytes() for task in tasks])


def publish_retry(task: ChunkTask) -> None:
    """Schedule a failed chunk for another attempt after its backoff delay."""
    queue = retry_queue_name(task.attempt)
    _publish(RETRY_EXCHANGE, queue, [task.next_attempt().to_bytes()])
    logger.info("chunk %d of job %s retrying via %s",
                task.chunk_index, task.job_id, queue)


def publish_failed(task: ChunkTask, error: str) -> None:
    """Park a chunk that exhausted its attempts on the failed queue."""
    _publish(DLX_EXCHANGE, config.RABBITMQ_QUEUE,
             [json.dumps({**asdict(task), "error": error[:2000]}).encode()])


def consume(handler: Callable[[ChunkTask], None], should_stop: Callable[[], bool]) -> None:
    """
    Consume chunk tasks until ``should_stop()`` returns True.

    Uses ``consume`` with an inactivity timeout rather than ``start_consuming``
    so the loop regains control periodically — otherwise a SIGTERM could not be
    honoured until the next message arrived.

    Messages are acked only after the handler returns. A worker that dies
    mid-chunk therefore has its message redelivered to another worker, which is
    what replaces lease expiry in a database-polled queue.
    """
    ch = channel()
    ch.basic_qos(prefetch_count=config.RABBITMQ_PREFETCH)

    for method, _properties, body in ch.consume(
        config.RABBITMQ_QUEUE,
        inactivity_timeout=config.RABBITMQ_CONSUME_TIMEOUT,
    ):
        if should_stop():
            break
        if method is None:      # inactivity timeout, no message
            continue

        try:
            task = ChunkTask.from_bytes(body)
        except Exception:
            # Unparseable message: ack it away rather than let it redeliver
            # forever, but keep a copy on the failed queue.
            logger.exception("dropping malformed message")
            ch.basic_publish(
                exchange=DLX_EXCHANGE,
                routing_key=config.RABBITMQ_QUEUE,
                body=body,
                properties=_PERSISTENT,
            )
            ch.basic_ack(method.delivery_tag)
            continue

        try:
            handler(task)
        finally:
            # Ack regardless: the handler owns its own retry decision and has
            # already republished if it wants another attempt. Nacking here
            # too would duplicate the message.
            ch.basic_ack(method.delivery_tag)

    try:
        ch.cancel()
    except Exception:
        logger.debug("error cancelling consumer", exc_info=True)


def queue_depth() -> Optional[int]:
    """Messages waiting on the work queue — the metric to autoscale workers on."""
    try:
        result = channel().queue_declare(
            config.RABBITMQ_QUEUE, durable=True, passive=True
        )
        return result.method.message_count
    except Exception:
        logger.warning("could not read queue depth", exc_info=True)
        return None


def health() -> bool:
    try:
        return connect().is_open
    except Exception:
        logger.warning("rabbitmq health check failed", exc_info=True)
        return False
