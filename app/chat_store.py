"""持久化当前活动 Chat ID，支持自动创建。"""
import json
import uuid
from pathlib import Path

from app.config import DEFAULT_CHAT_ID

_CHAT_FILE = Path("current-chat.json")


def get_current_chat_id() -> str | None:
    """返回持久化的 chat ID，没有则返回 None。"""
    try:
        if _CHAT_FILE.exists():
            data = json.loads(_CHAT_FILE.read_text(encoding="utf-8"))
            cid = data.get("chatId", "").strip()
            if cid:
                return cid
    except Exception as exc:
        print(f"Warning: failed to read current chat ID: {exc}")
    # 回退到环境变量配置
    return DEFAULT_CHAT_ID or None


def set_current_chat_id(chat_id: str) -> bool:
    try:
        _CHAT_FILE.write_text(
            json.dumps({"chatId": chat_id}, indent=2), encoding="utf-8"
        )
        print(f"✓ Current chat ID updated to: {chat_id}")
        return True
    except Exception as exc:
        print(f"Error: failed to save current chat ID: {exc}")
        return False


async def _parse_cookies(cookies_str: str) -> list[dict]:
    """将 cookie 字符串解析为 Playwright 可用的 dict 列表。"""
    result = []
    for part in cookies_str.split(";"):
        part = part.strip()
        if "=" in part:
            name, _, value = part.partition("=")
            result.append({
                "name": name.strip(),
                "value": value.strip(),
                "domain": "workspace.nexos.ai",
                "path": "/",
            })
    return result


async def browser_create_chat(cookies: str) -> tuple[str, str | None, bool]:
    """
    通过 httpx 调用 /api/chat/chats 获取最新的已有 chat_id。
    若列表为空则回退到 UUID。返回 (chat_id, last_message_id, is_real_chat)。
    is_real_chat=False 表示 UUID fallback，Nexos 服务器上没有该 chat。
    """
    from app.config import BASE_URL, COMMON_HEADERS
    from app.nexos_client import make_client

    headers = {
        "accept": "application/json, text/plain, */*",
        "user-agent": COMMON_HEADERS["user-agent"],
        "referer": f"{BASE_URL}/chat",
        "cookie": cookies,
    }
    try:
        async with make_client(timeout=15) as client:
            resp = await client.get(
                f"{BASE_URL}/api/chat/chats",
                params={"mode": "chat", "offset": 0, "limit": 1},
                headers=headers,
            )
            resp.raise_for_status()
            data = resp.json()
            items = data.get("items", [])
            if items:
                item = items[0]
                chat_id = item["id"]
                last_message_id = (
                    item.get("last_session", {}).get("message_id")
                    or item.get("last_session_message_id")
                )
                print(f"✓ browser_create_chat: reusing existing chat_id: {chat_id}, last_message_id: {last_message_id}")
                return chat_id, last_message_id, True
    except Exception as exc:
        print(f"Warning: browser_create_chat failed ({exc}), falling back to UUID generation")

    # 回退：生成 UUID（Nexos 服务器上不存在该 chat）
    chat_id = str(uuid.uuid4())
    print(f"✓ Generated new chat_id (UUID fallback): {chat_id}")
    return chat_id, None, False

async def create_new_chat(cookies: str) -> tuple[str, str | None, bool]:
    """
    获取/创建 Chat ID。
    优先复用 /api/chat/chats 列表中的最新 chat，失败则回退到 UUID。
    返回 (chat_id, last_message_id, is_real_chat)。
    """
    return await browser_create_chat(cookies)


async def get_or_create_chat_id(cookies: str) -> str:
    """返回当前 chat ID；若不存在则自动创建并持久化。"""
    cid = get_current_chat_id()
    if cid:
        return cid
    cid, _, _ = await create_new_chat(cookies)
    set_current_chat_id(cid)
    return cid
