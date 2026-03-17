"""Chat 管理接口：创建、切换、查询当前 Chat。"""
import re

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.chat_store import create_new_chat, get_current_chat_id, set_current_chat_id
from app.config import BASE_URL
from app.nexos_client import _get_cookies

router = APIRouter()


@router.get("/v1/chat/current")
async def get_current_chat():
    chat_id = get_current_chat_id()
    if not chat_id:
        return JSONResponse(status_code=404, content={"error": "No active chat. Call POST /v1/chat/new to create one."})
    return {"chatId": chat_id, "url": f"{BASE_URL}/chat/{chat_id}"}


@router.post("/v1/chat/switch")
async def switch_chat(request: Request):
    body = await request.json()
    chat_id = body.get("chatId")
    if not chat_id:
        return JSONResponse(status_code=400, content={"error": "chatId is required"})
    set_current_chat_id(chat_id)
    return {"success": True, "chatId": chat_id, "message": f"Switched to chat: {chat_id}"}


@router.post("/v1/chat/new")
async def create_chat(request: Request):
    """通过 GET /chat.data 在 nexos.ai 创建新对话（与原始 JS 逻辑一致）。"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    auto_switch: bool = body.get("auto_switch", True)

    try:
        cookies = _get_cookies()
    except RuntimeError as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

    try:
        new_chat_id = await create_new_chat(cookies)
    except RuntimeError as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

    if auto_switch:
        set_current_chat_id(new_chat_id)

    return {
        "success": True,
        "chatId": new_chat_id,
        "url": f"{BASE_URL}/chat/{new_chat_id}",
        "currentChat": auto_switch,
        "message": (
            f"New chat created and set as current: {new_chat_id}"
            if auto_switch
            else f"New chat created: {new_chat_id}"
        ),
    }
