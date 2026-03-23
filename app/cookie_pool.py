"""Cookie 池管理 —— 向下兼容的公共接口层。

内部委托给 cookie_store（SQLite 或 Redis），对外保持原有函数签名不变。

配置方式（.env）：
  # 方式一：多个 cookie（推荐）
  NEXOS_COOKIES_1='cookie_string_for_account_1'
  NEXOS_COOKIES_2='cookie_string_for_account_2'

  # 方式二：向下兼容单 cookie
  NEXOS_COOKIES='cookie_string'

  # 存储后端（可选）
  COOKIE_STORE_BACKEND=sqlite   # 默认，使用 SQLite 持久化
  COOKIE_STORE_BACKEND=redis    # 使用 Redis，需配合 REDIS_URL
  COOKIE_DB_PATH=cookies.db     # SQLite 文件路径
  REDIS_URL=redis://localhost:6379/0
  COOKIE_MAX_FAIL_COUNT=5       # 失败次数超阈值后自动停用
"""
import os

from app.config import COMMON_HEADERS  # noqa: F401 — 确保 dotenv 已加载
from app.cookie_store import CookieRecord, create_store

# 全局存储实例（模块加载时创建）
_store = create_store()


def _load_from_env() -> list[str]:
    """从环境变量读取 Cookie 列表（不写入存储，仅返回原始列表）。"""
    cookies: list[str] = []
    i = 1
    while True:
        val = os.getenv(f"NEXOS_COOKIES_{i}", "").replace("\r", "").replace("\n", "").strip()
        if not val:
            break
        cookies.append(val)
        i += 1
    if not cookies:
        val = os.getenv("NEXOS_COOKIES", "").replace("\r", "").replace("\n", "").strip()
        if val:
            cookies.append(val)
    return cookies


def _bootstrap() -> None:
    """启动时将 env 中的 Cookie 导入存储（INSERT OR IGNORE，不覆盖已有记录）。"""
    env_cookies = _load_from_env()
    if env_cookies:
        added = _store.add_cookies(env_cookies)
        if added:
            print(f"[cookie_pool] 从环境变量导入 {added} 个新 Cookie")


# 模块加载时自动执行一次 bootstrap
_bootstrap()


# ─────────────────────────────────────────────────
# 公共 API（与原版签名完全兼容）
# ─────────────────────────────────────────────────

def get_next() -> str:
    """Round-robin 取下一个活跃 Cookie。无可用 Cookie 时抛出 RuntimeError。"""
    return _store.get_next()


def get_next_with_chat() -> tuple[str, str | None]:
    """Round-robin 取下一个活跃 Cookie 及其绑定的 chat_id（None 表示未绑定）。"""
    return _store.get_next_with_chat()


def set_chat_id(cookie: str, chat_id: str) -> None:
    """将 chat_id 绑定到指定 cookie。"""
    _store.set_chat_id(cookie, chat_id)


def get_chat_id(cookie: str) -> str | None:
    """查询指定 cookie 当前绑定的 chat_id，无绑定返回 None。"""
    return _store.get_chat_id(cookie)


def clear_chat_id(cookie: str) -> None:
    """清除指定 cookie 绑定的 chat_id（chat 在服务器上已失效时调用）。"""
    _store.clear_chat_id(cookie)


def reload() -> None:
    """重新从环境变量导入 Cookie（运行时热更新）。"""
    _bootstrap()


def size() -> int:
    """返回当前活跃 Cookie 数量。"""
    return _store.size()


def mark_failed(cookie: str) -> None:
    """标记 Cookie 请求失败；累计超阈值后自动停用。"""
    _store.mark_failed(cookie)


def mark_success(cookie: str) -> None:
    """标记 Cookie 请求成功，重置失败计数。"""
    _store.mark_success(cookie)


def list_all() -> list[CookieRecord]:
    """返回所有 Cookie 记录（供管理接口使用）。"""
    return _store.list_all()


def add_cookies(cookies: list[str]) -> int:
    """动态添加 Cookie，返回实际新增数量。"""
    return _store.add_cookies(cookies)

def update_cookie(cookie: str, label: str | None = None, priority: int | None = None, is_active: bool | None = None) -> bool:
    """更新 Cookie 元信息（label/priority/is_active）。返回是否找到记录。"""
    return _store.update_cookie(cookie, label, priority, is_active)


def delete_cookie(cookie: str) -> bool:
    """从存储中永久删除指定 Cookie。返回是否找到记录。"""
    return _store.delete_cookie(cookie)


def record_health(cookie: str, healthy: bool) -> None:
    """记录健康检查结果（last_health_check_at / health_status）。"""
    _store.record_health(cookie, healthy)


def reload() -> int:
    """重新从环境变量导入 Cookie，返回新增数量。"""
    env_cookies = _load_from_env()
    if not env_cookies:
        return 0
    added = _store.add_cookies(env_cookies)
    if added:
        print(f"[cookie_pool] reload：新增 {added} 个 Cookie")
    return added
