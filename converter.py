import logging
import os
import os.path
import time
from urllib.parse import urlparse

from markitdown import MarkItDown, DocumentConverterResult
from openai import OpenAI
import requests
import tempfile

logger = logging.getLogger(__name__)

client = OpenAI(
    base_url="https://ai-gateway.vercel.sh/v1",
    api_key=os.environ["OPENAI_API_KEY"],
)
model = os.environ.get("OPENAI_MODEL", "google/gemini-3.1-flash-lite-preview")

# Seconds to wait for the connection and for each chunk of the response body.
DOWNLOAD_CONNECT_TIMEOUT = float(os.environ.get("DOWNLOAD_CONNECT_TIMEOUT", "10"))
DOWNLOAD_READ_TIMEOUT = float(os.environ.get("DOWNLOAD_READ_TIMEOUT", "30"))
# Hard cap on the downloaded file size, in bytes (default 100 MiB).
MAX_DOWNLOAD_BYTES = int(os.environ.get("MAX_DOWNLOAD_BYTES", str(100 * 1024 * 1024)))

_CHUNK_SIZE = 64 * 1024


def download(url: str) -> str:
    """
    Download a URL and return a file path.

    Args:
        url (str): The URL to download.

    Returns:
        str: The temporary file path where the downloaded content is stored.
    """
    with requests.get(
            url,
            stream=True,
            timeout=(DOWNLOAD_CONNECT_TIMEOUT, DOWNLOAD_READ_TIMEOUT),
    ) as response:
        if response.status_code != 200:
            raise Exception(f"Failed to download file from {url}, status code: {response.status_code}")

        # Extract filename from URL or use a default name
        parsed_url = urlparse(url)
        filename = os.path.basename(parsed_url.path)
        if not filename:
            filename = "downloaded_file"

        # Create a temporary file with a name derived from the URL
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
                    if written > MAX_DOWNLOAD_BYTES:
                        raise Exception(
                            f"File from {url} exceeds the maximum size of {MAX_DOWNLOAD_BYTES} bytes"
                        )
                    file.write(chunk)
        except Exception:
            if os.path.exists(temp_path):
                os.remove(temp_path)
            raise

        return temp_path


def convert(url: str) -> DocumentConverterResult:
    """
    Convert a URL to a MarkItDown object.

    This is fully synchronous and CPU/IO blocking; callers running inside an
    event loop must offload it to a worker thread.

    Args:
        url (str): The URL to convert.

    Returns:
        MarkItDown: The converted MarkItDown object.
    """
    started = time.monotonic()
    temp_file = download(url)
    downloaded_at = time.monotonic()
    logger.info("downloaded %s in %.3fs", url, downloaded_at - started)

    md = MarkItDown(llm_client=client, llm_model=model)
    try:
        converted = md.convert(temp_file)
    finally:
        if os.path.exists(temp_file):
            os.remove(temp_file)

    logger.info(
        "converted %s in %.3fs (download %.3fs, convert %.3fs)",
        url,
        time.monotonic() - started,
        downloaded_at - started,
        time.monotonic() - downloaded_at,
    )
    return converted
