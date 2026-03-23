"""异步 Nexos.ai HTTP 客户端封装。"""
import json
import re
import uuid
from typing import AsyncIterator

import httpx

from app.config import BASE_URL, COMMON_HEADERS, DEFAULT_HANDLER_ID, NEXOS_VERIFY_SSL, PROXY_URL


def build_headers(chat_id: str, cookies: str, extra: dict | None = None) -> dict:
    """构建请求头，cookies 由调用方显式传入。"""
    headers = {
        **COMMON_HEADERS,
        "origin": BASE_URL,
        "referer": f"{BASE_URL}/chat/{chat_id}",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "cookie": cookies,
    }
    if extra:
        headers.update(extra)
    return headers


async def resolve_handler_id(model: str, cookies: str) -> str:
    """动态查找 model -> handler ID，失败时返回 DEFAULT_HANDLER_ID。"""
    from app.model_registry import get_handler_id
    return await get_handler_id(model, cookies)


def make_client(**kwargs) -> "httpx.AsyncClient":
    """统一工厂：所有 AsyncClient 通过此处创建，集中管理 SSL/超时/重定向/代理配置。"""
    kwargs.setdefault("verify", NEXOS_VERIFY_SSL)
    kwargs.setdefault("follow_redirects", True)
    if PROXY_URL and "proxy" not in kwargs:
        kwargs["proxy"] = PROXY_URL
    return httpx.AsyncClient(**kwargs)


def generate_message_id() -> str:
    return str(uuid.uuid4())


def build_nexos_payload(
    chat_id: str,
    handler_id: str,
    user_text: str,
    last_message_id: str | None,
    temperature: float | None,
    max_tokens: int | None,
    is_real_chat: bool = True,
) -> dict:
    # 只在真实 chat（服务器上存在）时用 type=model 精确指定模型
    # UUID fallback 的 chat 不存在于服务器，必须用 type=auto
    handler_spec: dict = (
        {"type": "model", "id": handler_id, "fallbacks": True}
        if handler_id and is_real_chat
        else {"type": "auto", "fallbacks": True}
    )
    payload: dict = {
        "handler": handler_spec,
        "user_message": {
            "text": user_text,
            "client_metadata": {},
            "files": [],
        },
        "functionalityHeader": "chat",
        "tools": {
            "web_search": {"enabled": False},
            "deep_research": {"enabled": False},
            "code_interpreter": {"enabled": True},
        },
        "enabled_integrations": [],
        "chat": {},
    }
    if last_message_id:
        payload["chat"]["last_message_id"] = last_message_id
    return payload


def replace_image_links(
    text: str, chat_id: str, server_host: str, file_mapping: dict[str, str]
) -> str:
    """将 sandbox:// 图片路径替换为本地代理 URL。"""
    def _repl(m: re.Match) -> str:
        alt, filename = m.group(1), m.group(2)
        file_uuid = file_mapping.get(filename)
        if file_uuid:
            proxy = f"http://{server_host}/v1/files/{chat_id}/{file_uuid}/download"
            print(f"✓ Replaced image link: {filename} -> {proxy}")
            return f"![{alt}]({proxy})"
        return m.group(0)

    text = re.sub(
        r"!\[([^\]]*)\]\(sandbox:/mnt/output-data/([^)]+)\)", _repl, text
    )
    nexos_pattern = (
        re.escape(BASE_URL)
        + r"/api/chat/[a-f0-9-]{36}/files/([a-f0-9-]{36})/download"
    )
    text = re.sub(
        nexos_pattern,
        lambda m: f"http://{server_host}/v1/files/{chat_id}/{m.group(1)}/download",
        text,
    )
    return text


async def init_chat_on_server(
    client: httpx.AsyncClient,
    chat_id: str,
    cookies: str,
) -> str | None:
    """在 nexos 服务器上初始化 chat，返回 last_session_message_id（若有）。"""
    try:
        resp = await client.get(
            f"{BASE_URL}/api/chat/{chat_id}/chat",
            headers=build_headers(chat_id, cookies),
            timeout=30,
        )
        if resp.status_code == 200:
            data = resp.json()
            msg_id = data.get("last_session_message_id") or data.get("lastSessionMessageId")
            return msg_id
    except Exception:
        pass
    return None


async def download_file(
    client: httpx.AsyncClient, chat_id: str, file_id: str, cookies: str
) -> tuple[int, dict, bytes]:
    """下载 nexos 文件，返回 (status, headers, content)。"""
    resp = await client.get(
        f"{BASE_URL}/api/chat/{chat_id}/files/{file_id}/download",
        headers=build_headers(chat_id, cookies),
        follow_redirects=True,
    )
    return resp.status_code, dict(resp.headers), resp.content
