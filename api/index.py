from fastapi import FastAPI, HTTPException, Depends, Header
from converter import convert
from pydantic import BaseModel
from typing import Optional
import os

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
    try:
        result = convert(request.file)
        return {"content": result.text_content}
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Error processing URL: {str(e)}")


@app.get("/")
async def root():
    return {"status": "ok"}
