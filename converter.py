import os
import os.path
from urllib.parse import urlparse

from markitdown import MarkItDown, DocumentConverterResult
from openai import OpenAI
import requests
import tempfile

client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=os.environ["OPENAI_API_KEY"],
)
model = "google/gemini-2.5-flash-preview-05-20"


def download(url: str) -> str:
    """
    Download a URL and return a file path.

    Args:
        url (str): The URL to download.

    Returns:
        str: The temporary file path where the downloaded content is stored.
    """
    response = requests.get(url)
    if response.status_code == 200:
        # Extract filename from URL or use a default name
        parsed_url = urlparse(url)
        filename = os.path.basename(parsed_url.path)
        if not filename:
            filename = "downloaded_file"

        # Create a temporary file with a name derived from the URL
        fd, temp_path = tempfile.mkstemp(suffix=f"_{filename}")
        os.close(fd)

        # Write content to the temporary file
        with open(temp_path, 'wb') as file:
            file.write(response.content)
        return temp_path
    else:
        raise Exception(f"Failed to download file from {url}, status code: {response.status_code}")


def convert(url: str) -> DocumentConverterResult:
    """
    Convert a URL to a MarkItDown object.

    Args:
        url (str): The URL to convert.

    Returns:
        MarkItDown: The converted MarkItDown object.
    """
    temp_file = download(url)
    md = MarkItDown(llm_client=client, llm_model=model)
    converted = md.convert(temp_file)
    if os.path.exists(temp_file):
        os.remove(temp_file)
    return converted
