"""文件下载代理：/v1/files/{chat_id}/{file_id}/download"""
import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from app.nexos_client import _get_cookies, download_file, make_client

router = APIRouter()


@router.get("/v1/files/{chat_id}/{file_id}/download")
async def proxy_file(chat_id: str, file_id: str, request: Request):
    print(f"\n=== File download request ===")
    print(f"Chat ID: {chat_id}, File ID: {file_id}")

    try:
        cookies = _get_cookies()
    except RuntimeError as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

    async with make_client(timeout=60) as client:
        status, headers, content = await download_file(client, chat_id, file_id, cookies)

    forward_headers = {
        k: v
        for k, v in headers.items()
        if k.lower() in ("content-type", "content-disposition")
    }
    print(f"File download complete, size: {len(content)} bytes, status: {status}")
    return Response(content=content, status_code=status, headers=forward_headers)
