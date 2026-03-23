"""POST /v1/chat/completions — OpenAI 兼容接口。"""
import asyncio
import json
import os
import time
from typing import AsyncIterator

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.chat_store import create_new_chat
from app.config import BASE_URL, DISABLE_HISTORY
from app.sanitizer import sanitize_text
from app.cookie_pool import clear_chat_id, get_chat_id, get_next_with_chat, mark_failed, mark_success, set_chat_id
from app.request_logger import get_logger
from app.nexos_client import (
    build_headers,
    build_nexos_payload,
    generate_message_id,
    init_chat_on_server,
    make_client,
    replace_image_links,
    resolve_handler_id,
)

router = APIRouter()

# per-cookie 独立锁：仅用于首次 chat_id 创建，防并发重复获取
_chat_init_locks: dict[str, asyncio.Lock] = {}
# 正在处理请求的 cookie 集合（cookie 前 32 字符为键）
_busy_cookies: set[str] = set()
# 全局调度锁：原子化「选 Cookie + 标记 busy」，消除并发竞态
_dispatch_lock = asyncio.Lock()


class _ChatNotFoundError(Exception):
    """Nexos 返回 404 Chat not found，需要重建 chat_id。"""

def _server_host(request: Request) -> str:
    return request.headers.get("host") or f"{os.getenv('HOST', '0.0.0.0')}:{os.getenv('PORT', '3000')}"


def _extract_user_text(messages: list[dict]) -> str | None:
    """提取最后一条 user 消息的文本内容。"""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, list):
                # 多模态内容，拼接所有 text 块
                parts = [c.get("text", "") for c in content if c.get("type") == "text"]
                return "\n".join(parts)
            return str(content)
    return None


async def _nexos_stream(
    client: httpx.AsyncClient,
    chat_id: str,
    payload: dict,
    cookies: str,
) -> AsyncIterator[bytes]:
    async with client.stream(
        "POST",
        f"{BASE_URL}/api/chat/{chat_id}",
        files=[
            ("action", (None, json.dumps("chat_completion"), "text/plain")),
            ("chatId", (None, json.dumps(chat_id), "text/plain")),
            ("data", (None, json.dumps(payload), "text/plain")),
        ],
        headers=build_headers(chat_id, cookies),
        timeout=120,
    ) as resp:
        if resp.status_code != 200:
            await resp.aread()
            if resp.status_code == 404:
                raise _ChatNotFoundError()
            return
        async for chunk in resp.aiter_bytes():
            yield chunk


async def _collect_response(
    client: httpx.AsyncClient,
    chat_id: str,
    payload: dict,
    cookies: str,
) -> bytes:
    """收集全部响应字节（非流式）。"""
    chunks: list[bytes] = []
    async for chunk in _nexos_stream(client, chat_id, payload, cookies):
        chunks.append(chunk)
    return b"".join(chunks)


def _parse_sse_events(raw: bytes) -> list[dict]:
    """解析 SSE 字节流，返回所有 JSON 事件。"""
    events: list[dict] = []
    for line in raw.decode("utf-8", errors="replace").splitlines():
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data in ("", "[DONE]"):
            continue
        try:
            events.append(json.loads(data))
        except json.JSONDecodeError:
            pass
    return events


def _extract_text_and_files(
    events: list[dict],
) -> tuple[str, dict[str, str]]:
    """从 SSE 事件列表中提取文本内容和文件映射 {filename: file_uuid}。"""
    text_parts: list[str] = []
    file_mapping: dict[str, str] = {}

    for event in events:
        # 文件映射
        for tool_result in event.get("tool_results", []):
            for output_file in tool_result.get("output", {}).get("files", []):
                fname = output_file.get("name", "")
                fid = output_file.get("id", "")
                if fname and fid:
                    file_mapping[fname] = fid

        # 文本增量：Nexos 格式为 content.text，兼容 delta.text
        content = event.get("content")
        if isinstance(content, dict):
            text = content.get("text", "")
            if text:
                text_parts.append(text)
        elif isinstance(content, str) and content:
            text_parts.append(content)
        else:
            delta = event.get("delta", {})
            if isinstance(delta, dict):
                text = delta.get("text") or delta.get("content", "")
                if text:
                    text_parts.append(text)

    return "".join(text_parts), file_mapping


async def _stream_openai(
    gen: AsyncIterator[bytes],
    model: str,
    chat_id: str,
    server_host: str,
) -> AsyncIterator[str]:
    """将 nexos SSE 转换为 OpenAI SSE 格式。"""
    buf = ""
    file_mapping: dict[str, str] = {}
    chunks_sent = 0

    async for raw_chunk in gen:
        buf += raw_chunk.decode("utf-8", errors="replace")
        lines = buf.split("\n")
        buf = lines.pop()  # 保留未完成的行

        for line in lines:
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                continue
            try:
                event = json.loads(data)
            except json.JSONDecodeError:
                continue

            # 收集文件映射
            for tool_result in event.get("tool_results", []):
                for output_file in tool_result.get("output", {}).get("files", []):
                    fname = output_file.get("name", "")
                    fid = output_file.get("id", "")
                    if fname and fid:
                        file_mapping[fname] = fid
                        print(f"✓ File mapping: {fname} -> {fid}")

            # 提取文本增量（Nexos 格式：content.text，兼容 delta.text）
            text = ""
            content_field = event.get("content")
            if isinstance(content_field, dict):
                text = content_field.get("text", "")
            elif isinstance(content_field, str):
                text = content_field
            else:
                delta = event.get("delta")
                if isinstance(delta, str):
                    text = delta
                elif isinstance(delta, dict):
                    text = delta.get("text") or delta.get("content", "")
                elif isinstance(event.get("text"), str):
                    text = event["text"]

            if not text:
                continue

            text = replace_image_links(text, chat_id, server_host, file_mapping)
            text = sanitize_text(text)

            chunk = {
                "id": f"chatcmpl-{generate_message_id()}",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model,
                "choices": [{
                    "index": 0,
                    "delta": {"content": text},
                    "finish_reason": None,
                }],
            }
            yield f"data: {json.dumps(chunk)}\n\n"
            chunks_sent += 1

    # 处理缓冲区剩余
    if buf.strip():
        for line in buf.splitlines():
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data in ("", "[DONE]"):
                continue
            try:
                event = json.loads(data)
                text = ""
                content_field = event.get("content")
                if isinstance(content_field, dict):
                    text = content_field.get("text", "")
                elif isinstance(content_field, str):
                    text = content_field
                else:
                    delta = event.get("delta")
                    if isinstance(delta, str):
                        text = delta
                    elif isinstance(delta, dict):
                        text = delta.get("text") or delta.get("content", "")
                    elif isinstance(event.get("text"), str):
                        text = event["text"]
                if text:
                    text = replace_image_links(text, chat_id, server_host, file_mapping)
                    text = sanitize_text(text)
                    chunk = {
                        "id": f"chatcmpl-{generate_message_id()}",
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": model,
                        "choices": [{
                            "index": 0,
                            "delta": {"content": text},
                            "finish_reason": None,
                        }],
                    }
                    yield f"data: {json.dumps(chunk)}\n\n"
                    chunks_sent += 1
            except json.JSONDecodeError:
                pass

    print(f"Stream complete, chunks sent: {chunks_sent}")

    # 发送终止 chunk
    final = {
        "id": f"chatcmpl-{generate_message_id()}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    yield f"data: {json.dumps(final)}\n\n"
    yield "data: [DONE]\n\n"


@router.post("/v1/chat/completions")
async def chat_completions(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "Invalid JSON body"})

    messages: list[dict] = body.get("messages", [])
    model: str = body.get("model") or "nexos-chat"
    temperature: float = body.get("temperature", 1)
    max_tokens: int | None = body.get("max_tokens")
    stream: bool = body.get("stream", False)

    user_text = _extract_user_text(messages)
    if not user_text:
        return JSONResponse(status_code=400, content={"error": "No user message found"})

    # 原子化「选 Cookie + 标记 busy」：防止并发协程同时选中同一 Cookie
    async with _dispatch_lock:
        try:
            cookies, bound_chat_id = get_next_with_chat()
        except RuntimeError as e:
            return JSONResponse(status_code=500, content={"error": str(e)})
        cookie_key = cookies[:32]
        if cookie_key in _busy_cookies:
            return JSONResponse(
                status_code=429,
                content={"error": "所有 Cookie 均繁忙，请稍后重试"},
                headers={"Retry-After": "5"},
            )
        _busy_cookies.add(cookie_key)

    # 确保设置阶段异常时也释放 busy 标记（流式/非流式路径各自的 finally 负责正常路径）
    async def _get_chat() -> tuple[str, str | None, bool]:
        """优先复用绑定的 chat_id；否则加 per-cookie 锁创建，防并发重复获取。
        若 DISABLE_HISTORY=true 则每次强制创建新 chat，不复用也不持久化。"""
        if DISABLE_HISTORY:
            cid, last_msg, is_real = await create_new_chat(cookies)
            return cid, last_msg, is_real
        if bound_chat_id:
            async with make_client(timeout=30) as _c:
                fresh_last_msg = await init_chat_on_server(_c, bound_chat_id, cookies)
            return bound_chat_id, fresh_last_msg, True
        # 用 cookie 前 32 字符作为锁键，防止并发请求重复创建 chat
        lock_key = cookies[:32]
        if lock_key not in _chat_init_locks:
            _chat_init_locks[lock_key] = asyncio.Lock()
        async with _chat_init_locks[lock_key]:
            # 二次检查：可能在等锁期间已被其他协程绑定
            already = get_chat_id(cookies)
            if already:
                print(f"Reusing chat_id bound during lock wait: {already}")
                async with make_client(timeout=30) as _c:
                    fresh_last_msg = await init_chat_on_server(_c, already, cookies)
                return already, fresh_last_msg, True
            cid, last_msg, is_real = await create_new_chat(cookies)
            if cid and is_real:
                set_chat_id(cookies, cid)
            return cid, last_msg, is_real

    async def _fresh_chat() -> tuple[str, str | None, bool]:
        """清除过期绑定，强制获取新 chat（404 重试路径）。"""
        clear_chat_id(cookies)
        cid, last_msg, is_real = await create_new_chat(cookies)
        if cid and is_real:
            set_chat_id(cookies, cid)
        return cid, last_msg, is_real

    try:
        chat_id, last_message_id, is_real_chat = await _get_chat()
        handler_id = await resolve_handler_id(model, cookies)
    except Exception:
        _busy_cookies.discard(cookie_key)
        raise

    # Gemini 模型最大 65536
    if max_tokens and "gemini" in model.lower() and max_tokens > 65536:
        max_tokens = 65536

    server_host = _server_host(request)
    _t0 = time.time()

    def _make_payload(cid: str, last_msg: str | None, is_real: bool) -> dict:
        return build_nexos_payload(
            chat_id=cid,
            handler_id=handler_id,
            user_text=user_text,
            last_message_id=last_msg,
            temperature=temperature,
            max_tokens=max_tokens if body.get("max_tokens") else None,
            is_real_chat=is_real,
        )


    if stream:
        async def _stream_with_client() -> AsyncIterator[str]:
            _cid, _last_msg, _is_real = chat_id, last_message_id, is_real_chat
            client = make_client(timeout=120)
            _failed = False
            try:
                try:
                    async for chunk in _stream_openai(
                        _nexos_stream(client, _cid, _make_payload(_cid, _last_msg, _is_real), cookies),
                        model, _cid, server_host,
                    ):
                        yield chunk
                except _ChatNotFoundError:
                    _cid, _last_msg, _is_real = await _fresh_chat()
                    async for chunk in _stream_openai(
                        _nexos_stream(client, _cid, _make_payload(_cid, _last_msg, _is_real), cookies),
                        model, _cid, server_host,
                    ):
                        yield chunk
            except Exception as _exc:
                _failed = True
                mark_failed(cookies)
                get_logger().record(cookies, model, False, int((time.time() - _t0) * 1000), str(_exc))
                raise
            finally:
                if not _failed:
                    mark_success(cookies)
                    get_logger().record(cookies, model, True, int((time.time() - _t0) * 1000))
                _busy_cookies.discard(cookie_key)
                await client.aclose()

        return StreamingResponse(
            _stream_with_client(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    # 非流式
    _failed = False
    try:
        try:
            async with make_client(timeout=120) as client:
                raw = await _collect_response(client, chat_id, _make_payload(chat_id, last_message_id, is_real_chat), cookies)
        except _ChatNotFoundError:
            chat_id, last_message_id, is_real_chat = await _fresh_chat()
            async with make_client(timeout=120) as client:
                raw = await _collect_response(client, chat_id, _make_payload(chat_id, last_message_id, is_real_chat), cookies)
    except Exception as _exc:
        _failed = True
        mark_failed(cookies)
        get_logger().record(cookies, model, False, int((time.time() - _t0) * 1000), str(_exc))
        raise
    finally:
        if not _failed:
            mark_success(cookies)
            get_logger().record(cookies, model, True, int((time.time() - _t0) * 1000))
        _busy_cookies.discard(cookie_key)

    events = _parse_sse_events(raw)
    text, file_mapping = _extract_text_and_files(events)
    text = replace_image_links(text, chat_id, server_host, file_mapping)

    return JSONResponse({
        "id": f"chatcmpl-{generate_message_id()}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": text or "No response"},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    })
