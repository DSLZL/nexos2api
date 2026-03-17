"""动态从 Nexos /api/model-likes 获取并缓存模型列表（按 cookie 独立缓存）。"""
import asyncio
import re
import time

import httpx

from app.config import BASE_URL, COMMON_HEADERS
from app.nexos_client import make_client

_CACHE_TTL = 600  # 10 分钟

# 按 cookie 独立缓存：cookie_key -> (cached_at, mapping, models_list)
_cache: dict[str, tuple[float, dict[str, str], list[dict]]] = {}
_cache_lock = asyncio.Lock()


def _normalize(name: str) -> str:
    """'Claude Haiku 4.5' -> 'claude-haiku-4-5'"""
    return re.sub(r"[\s_.]+", "-", name.strip().lower())


def _cookie_key(cookies: str) -> str:
    """取 cookie 字符串前 32 个字符作为缓存键（避免完整 cookie 占内存）。"""
    return cookies[:32]


async def _fetch_models(cookies: str) -> tuple[dict[str, str], list[dict]]:
    """请求 /api/model-likes 并解析。

    只传 cookie，不加多余请求头，与浏览器行为一致。
    """
    headers = {
        "accept": "application/json, text/plain, */*",
        "user-agent": COMMON_HEADERS["user-agent"],
        "cookie": cookies,
    }
    async with make_client(timeout=20) as client:
        resp = await client.get(f"{BASE_URL}/api/model-likes", headers=headers)
        resp.raise_for_status()
        data = resp.json()

    mapping: dict[str, str] = {}
    models: list[dict] = []
    now = int(time.time())

    for item in data.get("userModels", []):
        model_info: dict = item.get("model", {})
        # model.id 是真实的 handler ID，item.id 只是 model-like 的 UUID
        handler_id: str = model_info.get("id", "")
        custom_name: str = model_info.get("custom_name", "")
        if not handler_id or not custom_name:
            continue

        normalized = _normalize(custom_name)
        mapping[normalized] = handler_id
        # 也允许用 handler UUID 直接指定模型
        mapping[handler_id] = handler_id

        models.append({
            "id": normalized,
            "object": "model",
            "created": now,
            "owned_by": "nexos",
            "name": custom_name,
        })

    return mapping, models


async def get_model_mapping(cookies: str) -> dict[str, str]:
    """返回该 cookie 对应的缓存 mapping，过期则自动刷新。"""
    key = _cookie_key(cookies)
    async with _cache_lock:
        cached_at, mapping, _ = _cache.get(key, (0.0, {}, []))
        if time.time() - cached_at > _CACHE_TTL:
            try:
                mapping, models = await _fetch_models(cookies)
                _cache[key] = (time.time(), mapping, models)
                print(f"✓ Model registry refreshed for cookie={key!r}: {len(mapping)} entries")
            except Exception as exc:
                print(f"Warning: failed to refresh model registry: {exc}")
                # 保留旧缓存，避免因网络抖动清空
    return _cache[key][1] if key in _cache else {}


async def get_models_list(cookies: str) -> list[dict]:
    """返回该 cookie 对应的模型列表。"""
    key = _cookie_key(cookies)
    await get_model_mapping(cookies)  # 确保缓存已刷新
    return _cache[key][2] if key in _cache else []


async def get_handler_id(model: str, cookies: str) -> str:
    """将模型名/UUID 解析为 handler ID，找不到时返回空字符串。"""
    from app.config import DEFAULT_HANDLER_ID
    mapping = await get_model_mapping(cookies)
    normalized = _normalize(model)
    result = mapping.get(normalized) or mapping.get(model)
    if not result:
        print(f"Warning: model '{model}' not found in registry, using DEFAULT_HANDLER_ID")
        return DEFAULT_HANDLER_ID
    return result
