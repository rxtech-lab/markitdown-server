"""
Full-stack pass: real PDF, real HTTP download, real markitdown conversion.

Everything else stubs conversion to stay fast. This one does not — it is the
test that proves the pieces actually fit together, so it is deliberately the
slow one.
"""
import functools
import http.server
import threading

import pytest

import config
import jobstore
import storage
import worker

pytestmark = pytest.mark.slow


@pytest.fixture
def pdf_server(tmp_path, make_pdf):
    """Serve a generated PDF over real HTTP on localhost."""
    make_pdf(25, name="book.pdf")

    handler = functools.partial(
        http.server.SimpleHTTPRequestHandler, directory=str(tmp_path)
    )
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/book.pdf"
    finally:
        server.shutdown()


@pytest.fixture
def client(database, s3, monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    import api.index as index

    monkeypatch.setattr(config, "ADMIN_API_KEY", "k")
    monkeypatch.setattr(config, "SOURCE_CACHE_DIR", str(tmp_path / "srccache"))
    # Force the 25-page document to actually split, so assembly across chunk
    # boundaries is exercised rather than short-circuited.
    monkeypatch.setattr(config, "PAGES_PER_CHUNK", 10)
    monkeypatch.setattr(config, "MIN_PAGES_FOR_PARALLEL", 5)

    with TestClient(index.app) as test_client:
        yield test_client


def test_converts_a_real_pdf_end_to_end(client, pdf_server, database, s3, broker):
    headers = {"x-api-key": "k"}

    submitted = client.post(
        "/async/convert", json={"file": pdf_server}, headers=headers
    )
    assert submitted.status_code == 202
    body = submitted.json()
    assert body["total_chunks"] == 3          # 25 pages at 10/chunk
    job_id, doc_key = body["job_id"], body["doc_key"]

    # Two workers race for the three chunks, as they would across pods.
    threads = [
        threading.Thread(target=_drain, args=(broker,)) for _ in range(2)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=300)

    status = client.get(f"/convert/jobs/{job_id}", headers=headers).json()
    assert status["status"] == "done", status
    assert status["progress"] == {"completed": 3, "total": 3, "percent": 100}

    # Walk every page through the HTTP API and reassemble it.
    manifest = storage.get_manifest(doc_key)
    text = ""
    for page in range(1, manifest["total_pages"] + 1):
        response = client.get(f"/convert/{doc_key}/pages/{page}", headers=headers)
        assert response.status_code == 200
        text += response.json()["content"]

    # Every page of the source survived...
    missing = [n for n in range(1, 26) if f"PAGEMARKER{n:05d}" not in text]
    assert not missing, f"lost pages: {missing}"

    # ...and they are still in order across chunk boundaries.
    positions = [text.index(f"PAGEMARKER{n:05d}") for n in range(1, 26)]
    assert positions == sorted(positions)


def test_resubmitting_hits_the_cache(broker, client, pdf_server, database, s3):
    headers = {"x-api-key": "k"}

    client.post("/async/convert", json={"file": pdf_server}, headers=headers)
    _drain(broker)

    # The original synchronous endpoint shares the same content-addressed
    # result and still returns the legacy first-page payload.
    again = client.post("/convert", json={"file": pdf_server}, headers=headers)
    assert again.status_code == 200          # served from cache, no new job
    assert "PAGEMARKER00001" in again.json()["content"]

    rows = database.query("SELECT COUNT(*) AS n FROM jobs")
    assert rows[0]["n"] == 1


def _drain(broker) -> None:
    broker.drain(worker.handle)
