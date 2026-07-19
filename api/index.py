from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.concurrency import run_in_threadpool
from converter import convert
from cache import store_document, get_page, PAGE_SIZE
from pydantic import BaseModel
from typing import Optional
import logging
import os

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI()


class ConvertRequest(BaseModel):
    file: Optional[str] = None


async def verify_api_key(x_api_key: str = Header(None)):
    admin_api_key = os.environ.get("ADMIN_API_KEY")
    if not admin_api_key:
        raise HTTPException(status_code=500, detail="API key not configured on server")

    if x_api_key != admin_api_key:
        raise HTTPException(status_code=401, detail="Invalid API key")

    return x_api_key


@app.post("/convert", dependencies=[Depends(verify_api_key)])
async def convert_endpoint(
        request: Optional[ConvertRequest] = None,
):
    """
    Convert a document at the given URL to markdown.

    The markdown is paginated and stored in Redis (5 min TTL). The response
    contains the first page plus pagination metadata; subsequent pages can be
    fetched from ``GET /convert/{id}/pages/{page}`` using the returned ``id``.
    """
    if request is None or not request.file:
        raise HTTPException(status_code=422, detail="`file` (a URL) is required")

    try:
        # `convert` blocks on network IO and CPU-bound parsing; running it on the
        # event loop would stall the health probes and get the pod killed.
        result = await run_in_threadpool(convert, request.file)
        return await run_in_threadpool(store_document, result.text_content)
    except Exception as e:
        logger.exception("conversion failed for %s", request.file)
        raise HTTPException(status_code=400, detail=f"Error processing URL: {str(e)}")


@app.get("/convert/{doc_id}/pages/{page}", dependencies=[Depends(verify_api_key)])
async def get_page_endpoint(doc_id: str, page: int):
    """
    Fetch a single page of a previously converted document.

    Returns 404 if the document has expired from the cache or the page is out
    of range.
    """
    result = await run_in_threadpool(get_page, doc_id, page)
    if result is None:
        raise HTTPException(
            status_code=404,
            detail="Document or page not found (it may have expired).",
        )
    return result


@app.get("/")
async def root():
    return {"status": "ok"}
