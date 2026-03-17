"""Cookie 池管理，支持多账号 round-robin 轮询。

配置方式（.env）：
  # 方式一：多个 cookie（推荐）
  NEXOS_COOKIES_1='cookie_string_for_account_1'
  NEXOS_COOKIES_2='cookie_string_for_account_2'

  # 方式二：向下兼容单 cookie
  NEXOS_COOKIES='cookie_string'
"""
import os
import threading

from app.config import COMMON_HEADERS  # noqa: F401 — 确保 dotenv 已加载

_pool: list[str] = []
_index: int = 0
_lock = threading.Lock()


def _load() -> list[str]:
    cookies: list[str] = []
    # NEXOS_COOKIES_1, NEXOS_COOKIES_2, ...
    i = 1
    while True:
        val = os.getenv(f"NEXOS_COOKIES_{i}", "").replace("\r", "").replace("\n", "").strip()
        if not val:
            break
        cookies.append(val)
        i += 1
    # 向下兼容：单 cookie
    if not cookies:
        val = os.getenv("NEXOS_COOKIES", "").replace("\r", "").replace("\n", "").strip()
        if val:
            cookies.append(val)
    return cookies


def reload() -> None:
    """重新从环境变量加载 cookie 池（用于测试或热更新）。"""
    global _pool, _index
    with _lock:
        _pool = _load()
        _index = 0
    print(f"✓ Cookie pool loaded: {len(_pool)} cookie(s)")


def get_next() -> str:
    """Round-robin 返回下一个 cookie，池为空则抛 RuntimeError。"""
    global _index
    with _lock:
        if not _pool:
            raise RuntimeError(
                "No cookies configured. "
                "Set NEXOS_COOKIES or NEXOS_COOKIES_1 / NEXOS_COOKIES_2 / ... in .env"
            )
        cookie = _pool[_index % len(_pool)]
        _index += 1
        return cookie


def size() -> int:
    return len(_pool)


# 模块加载时自动初始化
reload()
