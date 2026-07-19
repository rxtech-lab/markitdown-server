import os

import converter


def test_download_has_no_fixed_request_timeout(monkeypatch):
    request_options = None

    class Response:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def iter_content(self, chunk_size):
            assert chunk_size > 0
            yield b"document"

    def get(_url, **options):
        nonlocal request_options
        request_options = options
        return Response()

    monkeypatch.setattr(converter.requests, "get", get)

    path = converter.download("https://example.com/document.pdf")
    try:
        assert request_options == {"stream": True}
        with open(path, "rb") as downloaded:
            assert downloaded.read() == b"document"
    finally:
        os.remove(path)
