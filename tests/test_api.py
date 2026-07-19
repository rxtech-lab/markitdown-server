"""
The producer HTTP surface: submission, cache hits, job polling, page reads.
"""
import pytest
from fastapi.testclient import TestClient

import config
import jobstore
import storage
import worker

API_KEY = "test-key"
HEADERS = {"x-api-key": API_KEY}


def test_startup_does_not_provision_storage(monkeypatch):
    import api.index as index

    def fail_if_called():
        raise AssertionError("API startup must not provision object storage")

    monkeypatch.setattr(index.db, "migrate", lambda: None)
    monkeypatch.setattr(index.storage, "ensure_bucket", fail_if_called)

    with TestClient(index.app) as test_client:
        assert test_client.get("/").status_code == 200


@pytest.fixture
def client(database, s3, broker, fast_queue, monkeypatch, tmp_path):
    import api.index as index

    monkeypatch.setattr(config, "ADMIN_API_KEY", API_KEY)

    # Stand in for the network: "downloading" a URL yields a local file whose
    # bytes are derived from the URL, so distinct URLs hash distinctly.
    def fake_download(url: str) -> str:
        path = tmp_path / f"dl-{abs(hash(url))}.bin"
        path.write_bytes(f"content of {url}".encode())
        return str(path)

    monkeypatch.setattr(index.converter, "download", fake_download)
    monkeypatch.setattr(index.chunker, "page_count", lambda path: None)
    monkeypatch.setattr(
        worker, "convert_range",
        lambda file_hash, start, end, use_llm: "# converted markdown",
    )

    with TestClient(index.app) as test_client:
        yield test_client


def drain(broker, limit=200):
    """Deliver every queued chunk, as a worker fleet would."""
    broker.drain(worker.handle, limit=limit)


class TestAuth:
    def test_missing_key_is_rejected(self, client):
        assert client.post("/convert", json={"file": "http://x/a.pdf"}).status_code == 401

    def test_wrong_key_is_rejected(self, client):
        response = client.post(
            "/convert", json={"file": "http://x/a.pdf"},
            headers={"x-api-key": "nope"},
        )
        assert response.status_code == 401

    def test_async_route_also_requires_a_key(self, client):
        response = client.post("/async/convert", json={"file": "http://x/a.pdf"})
        assert response.status_code == 401

    def test_health_needs_no_key(self, client):
        assert client.get("/").status_code == 200


class TestAsyncSubmit:
    def test_returns_202_with_a_job_id(self, client):
        response = client.post(
            "/async/convert", json={"file": "http://x/a.pdf"}, headers=HEADERS
        )
        assert response.status_code == 202
        body = response.json()
        assert body["status"] == "queued"
        assert body["job_id"]
        assert body["total_chunks"] == 1

    def test_missing_file_is_422(self, client):
        response = client.post("/async/convert", json={}, headers=HEADERS)
        assert response.status_code == 422

    def test_download_failure_is_reported_synchronously(self, client, monkeypatch):
        """A bad URL is the caller's problem — better a 400 now than a job that
        instantly fails."""
        import api.index as index

        monkeypatch.setattr(
            index.converter, "download",
            lambda url: (_ for _ in ()).throw(Exception("404 from origin")),
        )
        response = client.post(
            "/async/convert",
            json={"file": "http://x/missing.pdf"},
            headers=HEADERS,
        )
        assert response.status_code == 400
        assert "404 from origin" in response.json()["detail"]


class TestJobStatus:
    def test_reports_progress_then_completion(self, broker, client):
        job_id = client.post(
            "/async/convert", json={"file": "http://x/a.pdf"}, headers=HEADERS
        ).json()["job_id"]

        queued = client.get(f"/convert/jobs/{job_id}", headers=HEADERS).json()
        assert queued["status"] == "queued"
        assert queued["progress"] == {"completed": 0, "total": 1, "percent": 0}

        drain(broker)

        done = client.get(f"/convert/jobs/{job_id}", headers=HEADERS).json()
        assert done["status"] == "done"
        assert done["progress"]["percent"] == 100

    def test_unknown_job_is_404(self, client):
        assert client.get("/convert/jobs/nope", headers=HEADERS).status_code == 404

    def test_failed_job_reports_its_error(self, client):
        job_id = client.post(
            "/async/convert", json={"file": "http://x/a.pdf"}, headers=HEADERS
        ).json()["job_id"]
        jobstore.fail_job(job_id, "parser exploded", 0)

        body = client.get(f"/convert/jobs/{job_id}", headers=HEADERS).json()
        assert body["status"] == "failed"
        assert body["error"] == "parser exploded"


class TestCacheHit:
    def test_resubmitting_the_same_file_skips_the_queue(self, broker, client):
        first = client.post(
            "/async/convert", json={"file": "http://x/a.pdf"}, headers=HEADERS
        )
        assert first.status_code == 202
        drain(broker)

        second = client.post(
            "/async/convert", json={"file": "http://x/a.pdf"}, headers=HEADERS
        )
        assert second.status_code == 200
        body = second.json()
        assert body["content"] == "# converted markdown"
        assert body["pagination"]["total_pages"] == 1

    def test_cache_hit_creates_no_new_job(self, broker, client, database):
        client.post(
            "/async/convert", json={"file": "http://x/a.pdf"}, headers=HEADERS
        )
        drain(broker)
        client.post(
            "/async/convert", json={"file": "http://x/a.pdf"}, headers=HEADERS
        )

        rows = database.query("SELECT COUNT(*) AS n FROM jobs")
        assert rows[0]["n"] == 1

    def test_different_files_do_not_share_a_cache_entry(self, broker, client):
        client.post(
            "/async/convert", json={"file": "http://x/a.pdf"}, headers=HEADERS
        )
        drain(broker)
        response = client.post(
            "/async/convert", json={"file": "http://x/b.pdf"}, headers=HEADERS
        )
        assert response.status_code == 202

    def test_llm_flag_does_not_share_a_cache_entry(self, broker, client):
        client.post(
            "/async/convert",
            json={"file": "http://x/a.pdf", "use_llm": False},
            headers=HEADERS,
        )
        drain(broker)
        response = client.post(
            "/async/convert",
            json={"file": "http://x/a.pdf", "use_llm": True},
            headers=HEADERS,
        )
        assert response.status_code == 202


class TestSyncConvert:
    def test_default_route_waits_and_returns_the_legacy_payload(
            self, broker, client,
    ):
        """Existing callers get the old response without changing their body."""
        import threading
        import time

        def convert_after_a_moment():
            time.sleep(0.2)
            drain(broker)

        threading.Thread(target=convert_after_a_moment, daemon=True).start()

        response = client.post(
            "/convert", json={"file": "http://x/a.pdf"},
            headers=HEADERS,
        )
        assert response.status_code == 200
        body = response.json()
        assert set(body) == {"id", "content", "pagination"}
        assert body["content"] == "# converted markdown"

    def test_surfaces_a_conversion_failure(self, broker, client, monkeypatch):
        import threading
        import time

        monkeypatch.setattr(config, "MAX_ATTEMPTS", 1)
        monkeypatch.setattr(
            worker, "convert_range",
            lambda *a: (_ for _ in ()).throw(RuntimeError("bad pdf")),
        )

        def fail_it():
            time.sleep(0.2)
            drain(broker)

        threading.Thread(target=fail_it, daemon=True).start()

        response = client.post(
            "/convert", json={"file": "http://x/a.pdf"},
            headers=HEADERS,
        )
        assert response.status_code == 400
        assert "bad pdf" in response.json()["detail"]


class TestPages:
    def test_reads_a_page(self, broker, client):
        doc_key = client.post(
            "/async/convert", json={"file": "http://x/a.pdf"}, headers=HEADERS
        ).json()["doc_key"]
        drain(broker)

        response = client.get(f"/convert/{doc_key}/pages/1", headers=HEADERS)
        assert response.status_code == 200
        assert response.json()["content"] == "# converted markdown"

    def test_out_of_range_page_is_404(self, broker, client):
        body = client.post(
            "/async/convert", json={"file": "http://x/a.pdf"}, headers=HEADERS
        ).json()
        drain(broker)
        assert client.get(
            f"/convert/{body['doc_key']}/pages/99", headers=HEADERS
        ).status_code == 404

    def test_unknown_document_is_404(self, client):
        assert client.get(
            "/convert/nosuchdoc/pages/1", headers=HEADERS
        ).status_code == 404


class TestReadiness:
    def test_reports_healthy_dependencies(self, client):
        body = client.get("/readyz").json()
        assert body["status"] == "ok"
        assert body["checks"] == {"database": True, "storage": True, "queue": True}

    def test_reports_degraded_when_storage_is_down(self, client, monkeypatch):
        import api.index as index

        monkeypatch.setattr(index.storage, "health", lambda: False)
        response = client.get("/readyz")
        assert response.status_code == 503
        assert response.json()["status"] == "degraded"


class TestOpenAPI:
    def test_yaml_needs_no_key(self, client):
        assert client.get("/openapi.yaml").status_code == 200

    def test_yaml_matches_the_json_document(self, client):
        import yaml

        spec = yaml.safe_load(client.get("/openapi.yaml").text)
        assert spec == client.get("/openapi.json").json()

    def test_documents_every_route(self, client):
        import yaml

        paths = yaml.safe_load(client.get("/openapi.yaml").text)["paths"]
        assert set(paths) == {
            "/", "/readyz", "/convert", "/async/convert",
            "/convert/jobs/{job_id}", "/convert/{doc_id}/pages/{page}",
        }

    def test_documents_sync_and_async_response_contracts(self, client):
        import yaml

        paths = yaml.safe_load(client.get("/openapi.yaml").text)["paths"]
        sync_responses = paths["/convert"]["post"]["responses"]
        assert "202" not in sync_responses
        assert sync_responses["200"]["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/PageResponse"
        }

        async_responses = paths["/async/convert"]["post"]["responses"]
        assert async_responses["200"]["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/PageResponse"
        }
        assert async_responses["202"]["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/JobAccepted"
        }

    def test_declares_the_api_key_security_scheme(self, client):
        import yaml

        spec = yaml.safe_load(client.get("/openapi.yaml").text)
        assert spec["components"]["securitySchemes"]["APIKeyHeader"] == {
            "type": "apiKey", "in": "header", "name": "x-api-key",
        }
