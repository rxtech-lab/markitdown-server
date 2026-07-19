import os
import threading

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


@pytest.fixture
def database(tmp_path, monkeypatch):
    """
    A fresh SQLite database per test.

    A real file rather than ":memory:" so the concurrency tests, which open a
    connection per thread, all see the same database and exercise real write
    contention.
    """
    import config
    import db

    monkeypatch.setattr(config, "DATABASE_URL", str(tmp_path / "test.db"))
    monkeypatch.setattr(config, "DATABASE_AUTH_TOKEN", None)
    db.reset()
    db.migrate()
    yield db
    db.reset()


@pytest.fixture
def s3(monkeypatch):
    """An in-memory S3 via moto, with the bucket already created."""
    import boto3
    from moto import mock_aws

    import config
    import storage

    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setattr(config, "S3_ENDPOINT", None)
    monkeypatch.setattr(config, "S3_REGION", "us-east-1")
    monkeypatch.setattr(config, "S3_BUCKET", "test-bucket")
    monkeypatch.setattr(config, "S3_ACCESS_KEY_ID", "test")
    monkeypatch.setattr(config, "S3_SECRET_ACCESS_KEY", "test")

    with mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="test-bucket")
        storage.reset()
        yield storage
        storage.reset()


class FakeBroker:
    """
    In-memory stand-in for RabbitMQ.

    Models only what the code depends on: FIFO delivery, one delivery at a
    time, and retry/failed queues that messages are republished onto. Delays
    are not simulated — retried messages become immediately available, since
    the delay is RabbitMQ's TTL and not our logic.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.ready: list = []
        self.retried: list = []
        self.failed: list = []
        self.published: list = []

    def publish(self, tasks):
        for task in tasks:
            self.ready.append(task)
            self.published.append(task)

    def publish_retry(self, task):
        nxt = task.next_attempt()
        self.retried.append(nxt)
        self.ready.append(nxt)

    def publish_failed(self, task, error):
        self.failed.append((task, error))

    def _take(self):
        """Pop one message, or None. Locked: the integration test drains from
        several threads, and two of them must never get the same message."""
        with self._lock:
            return self.ready.pop(0) if self.ready else None

    def consume(self, handler, should_stop):
        while not should_stop():
            task = self._take()
            if task is None:
                return
            handler(task)

    def drain(self, handler, limit: int = 200):
        """Deliver every message, including ones handlers republish."""
        for _ in range(limit):
            task = self._take()
            if task is None:
                return
            handler(task)
        raise AssertionError("drain did not converge — retry loop?")


@pytest.fixture
def broker(monkeypatch):
    """Replace RabbitMQ with an in-memory broker."""
    import taskqueue

    fake = FakeBroker()
    monkeypatch.setattr(taskqueue, "publish", fake.publish)
    monkeypatch.setattr(taskqueue, "publish_retry", fake.publish_retry)
    monkeypatch.setattr(taskqueue, "publish_failed", fake.publish_failed)
    monkeypatch.setattr(taskqueue, "consume", fake.consume)
    monkeypatch.setattr(taskqueue, "health", lambda: True)
    return fake


@pytest.fixture
def fast_queue(monkeypatch):
    """Collapse queue timings so tests do not sleep."""
    import config

    monkeypatch.setattr(config, "RABBITMQ_CONSUME_TIMEOUT", 0.01)
    monkeypatch.setattr(
        config, "WORKER_LIVENESS_FILE",
        os.path.join(os.environ.get("TMPDIR", "/tmp"), "worker-alive-test"),
    )
    return config
