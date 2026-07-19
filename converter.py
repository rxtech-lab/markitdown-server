"""
Downloading source documents and converting them to markdown.

The LLM client is built lazily. It used to be constructed at import time from a
required ``OPENAI_API_KEY``, which meant any process that merely imported this
module — including workers that never take the LLM path — crashed at startup
without a key it did not need.

markitdown is likewise imported lazily. It costs ~150-190 MiB of RSS, and the
API pod imports this module only to download and hash files.
"""
import functools
import logging
import os
import os.path
import tempfile
from urllib.parse import urlparse

import requests

import config

logger = logging.getLogger(__name__)

_CHUNK_SIZE = 64 * 1024


@functools.lru_cache(maxsize=1)
def get_llm_client():
    """
    Build the OpenAI client on first use.

    Raises only when the LLM path is actually taken, so a missing key is a
    per-request error rather than a startup crash.
    """
    from openai import OpenAI

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "OPENAI_API_KEY is not set, but LLM-assisted conversion was requested"
        )
    return OpenAI(base_url=config.OPENAI_BASE_URL, api_key=api_key)


def download(url: str) -> str:
    """
    Download a URL and return a temporary file path.

    Args:
        url (str): The URL to download.

    Returns:
        str: The temporary file path where the downloaded content is stored.
    """
    with requests.get(url, stream=True) as response:
        if response.status_code != 200:
            raise Exception(
                f"Failed to download file from {url}, "
                f"status code: {response.status_code}"
            )

        # Extract filename from URL or use a default name
        parsed_url = urlparse(url)
        filename = os.path.basename(parsed_url.path)
        if not filename:
            filename = "downloaded_file"

        fd, temp_path = tempfile.mkstemp(suffix=f"_{filename}")
        os.close(fd)

        # Stream the content to the temporary file so memory stays bounded
        written = 0
        try:
            with open(temp_path, 'wb') as file:
                for chunk in response.iter_content(chunk_size=_CHUNK_SIZE):
                    if not chunk:
                        continue
                    written += len(chunk)
                    if written > config.MAX_DOWNLOAD_BYTES:
                        raise Exception(
                            f"File from {url} exceeds the maximum size of "
                            f"{config.MAX_DOWNLOAD_BYTES} bytes"
                        )
                    file.write(chunk)
        except Exception:
            if os.path.exists(temp_path):
                os.remove(temp_path)
            raise

        return temp_path


def convert_file(path: str, use_llm: bool = False) -> str:
    """
    Convert a local file to markdown.

    The single conversion primitive. Previously the LLM was used or skipped
    depending on whether a document happened to cross the 40-page chunking
    threshold, so a 39-page PDF got image descriptions and a 40-page one did
    not — a behaviour flip no caller could predict. ``use_llm`` now says so
    explicitly and applies uniformly to every chunk of a document.
    """
    from markitdown import MarkItDown

    if use_llm:
        md = MarkItDown(llm_client=get_llm_client(), llm_model=config.OPENAI_MODEL)
    else:
        md = MarkItDown()
    return md.convert(path).markdown
