"""POST /v1/chat/completions — OpenAI 兼容接口。"""
import json
import os
import time
from typing import AsyncIterator

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.chat_store import create_new_chat
from app.config import BASE_URL
from app.nexos_client import (
    _get_cookies,
    build_headers,
    build_nexos_payload,
    generate_message_id,
    make_client,
    replace_image_links,
    resolve_handler_id,
)

router = APIRouter()


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
        print(f"Nexos response status: {resp.status_code}")
        if resp.status_code != 200:
            body = await resp.aread()
            print(f"Nexos error body: {body[:500]}")
            return
        async for chunk in resp.aiter_bytes():
            if chunk:
                print(f"Chunk ({len(chunk)} bytes): {chunk[:200]}")
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

    print("\n=== New chat request ===")
    print("Messages:", json.dumps(body.get("messages", []), ensure_ascii=False))

    messages: list[dict] = body.get("messages", [])
    model: str = body.get("model") or "nexos-chat"
    temperature: float = body.get("temperature", 1)
    max_tokens: int | None = body.get("max_tokens")
    stream: bool = body.get("stream", False)

    user_text = _extract_user_text(messages)
    if not user_text:
        return JSONResponse(status_code=400, content={"error": "No user message found"})
    print("User message:", user_text[:200])

    # 验证并获取 cookies
    try:
        cookies = _get_cookies()
    except RuntimeError as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

    # 每次请求获取最新 chat_id 及 last_session_message_id（一次请求完成）
    chat_id, last_message_id, is_real_chat = await create_new_chat(cookies)
    print(f"Chat ID: {chat_id}, last_message_id: {last_message_id}, is_real: {is_real_chat}")

    handler_id = await resolve_handler_id(model, cookies)
    print(f"Requested model: {model}, handler ID: {handler_id}")

    # Gemini 模型最大 65536
    if max_tokens and "gemini" in model.lower() and max_tokens > 65536:
        max_tokens = 65536
        print(f"Adjusted max_tokens to 65536 for Gemini model")

    server_host = _server_host(request)

    payload = build_nexos_payload(
        chat_id=chat_id,
        handler_id=handler_id,
        user_text=user_text,
        last_message_id=last_message_id,
        temperature=temperature,
        max_tokens=max_tokens if body.get("max_tokens") else None,
        is_real_chat=is_real_chat,
    )
    print("Nexos payload:", json.dumps(payload, ensure_ascii=False))

    if stream:
        # 流式：client 生命周期由生成器内部管理，不能用 async with 包裹
        async def _stream_with_client() -> AsyncIterator[str]:
            client = make_client(timeout=120)
            try:
                async for chunk in _stream_openai(
                    _nexos_stream(client, chat_id, payload, cookies),
                    model,
                    chat_id,
                    server_host,
                ):
                    yield chunk
            finally:
                await client.aclose()

        return StreamingResponse(
            _stream_with_client(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    # 非流式：正常 async with 即可
    async with make_client(timeout=120) as client:
        raw = await _collect_response(client, chat_id, payload, cookies)
    print(f"Response size: {len(raw)} bytes")

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
