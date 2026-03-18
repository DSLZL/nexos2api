"""Cookie 持久化存储层。

支持两种后端：
  - SQLiteStore（默认）：零依赖，WAL 模式，适合单机
  - RedisStore（可选）：原子 round-robin，适合多进程/分布式

通过环境变量 COOKIE_STORE_BACKEND=sqlite|redis 切换。
"""
from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Optional, Protocol, runtime_checkable

from app.config import (
    COOKIE_DB_PATH,
    COOKIE_MAX_FAIL_COUNT,
    COOKIE_STORE_BACKEND,
    REDIS_URL,
)


# ─────────────────────────────────────────────────
# 数据模型
# ─────────────────────────────────────────────────

@dataclass
class CookieRecord:
    id: int
    value: str
    is_active: bool
    fail_count: int
    last_used: Optional[float]
    created_at: float
    chat_id: str = ""


# ─────────────────────────────────────────────────
# 存储协议（接口）
# ─────────────────────────────────────────────────

@runtime_checkable
class CookieStore(Protocol):
    def add_cookies(self, cookies: list[str]) -> int: ...
    """批量导入 Cookie，返回实际新增数量（重复跳过）。"""

    def get_next(self) -> str: ...
    """Round-robin 取下一个活跃 Cookie；无可用 Cookie 时抛出 RuntimeError。"""

    def get_next_with_chat(self) -> tuple[str, str | None]: ...
    """Round-robin 取下一个活跃 Cookie 及其绑定的 chat_id（None 表示未绑定）。"""

    def set_chat_id(self, cookie: str, chat_id: str) -> None: ...
    """将 chat_id 绑定到指定 cookie。"""

    def get_chat_id(self, cookie: str) -> str | None: ...
    """查询指定 cookie 当前绑定的 chat_id，无绑定返回 None。"""

    def clear_chat_id(self, cookie: str) -> None: ...
    """清除指定 cookie 绑定的 chat_id（chat 失效时调用）。"""

    def mark_failed(self, cookie: str) -> None: ...
    """标记请求失败；累计失败超阈值后自动设为 inactive。"""

    def mark_success(self, cookie: str) -> None: ...
    """重置失败计数（请求成功后调用）。"""

    def list_all(self) -> list[CookieRecord]: ...
    """返回所有 Cookie 记录（管理用）。"""

    def size(self) -> int: ...
    """返回活跃 Cookie 数量。"""


# ─────────────────────────────────────────────────
# SQLite 实现
# ─────────────────────────────────────────────────

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS cookies (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    value      TEXT UNIQUE NOT NULL,
    is_active  INTEGER DEFAULT 1,
    fail_count INTEGER DEFAULT 0,
    last_used  REAL,
    created_at REAL NOT NULL,
    chat_id    TEXT DEFAULT ''
)
"""

_MIGRATE_ADD_CHAT_ID = "ALTER TABLE cookies ADD COLUMN chat_id TEXT DEFAULT ''"


class SQLiteStore:
    """基于 SQLite WAL 模式的 Cookie 持久化存储。

    WAL（Write-Ahead Logging）允许并发读不阻塞写，
    配合 threading.Lock 保证 round-robin 原子性。
    """

    def __init__(self, db_path: str = COOKIE_DB_PATH) -> None:
        self._db_path = db_path
        self._lock = threading.Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(_CREATE_TABLE)
            # 迁移：若旧表缺少 chat_id 列则补充
            cols = {row[1] for row in conn.execute("PRAGMA table_info(cookies)")}
            if "chat_id" not in cols:
                conn.execute(_MIGRATE_ADD_CHAT_ID)
            conn.commit()

    def add_cookies(self, cookies: list[str]) -> int:
        added = 0
        with self._lock, self._connect() as conn:
            for cookie in cookies:
                cookie = cookie.strip()
                if not cookie:
                    continue
                try:
                    conn.execute(
                        "INSERT INTO cookies (value, is_active, fail_count, created_at) "
                        "VALUES (?, 1, 0, ?)",
                        (cookie, time.time()),
                    )
                    added += 1
                except sqlite3.IntegrityError:
                    # UNIQUE 冲突：已存在，跳过
                    pass
            conn.commit()
        return added

    def get_next(self) -> str:
        """原子 round-robin：取 last_used 最旧（或 NULL）的活跃 Cookie。"""
        cookie, _ = self.get_next_with_chat()
        return cookie

    def get_next_with_chat(self) -> tuple[str, str | None]:
        """原子 round-robin：返回 (cookie, chat_id)，chat_id 为 None 表示尚未绑定。"""
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT id, value, chat_id FROM cookies "
                "WHERE is_active = 1 "
                "ORDER BY last_used ASC NULLS FIRST "
                "LIMIT 1"
            ).fetchone()
            if row is None:
                raise RuntimeError(
                    "Cookie 池为空或全部失效，请添加有效 Cookie。"
                )
            conn.execute(
                "UPDATE cookies SET last_used = ? WHERE id = ?",
                (time.time(), row["id"]),
            )
            conn.commit()
            chat_id = row["chat_id"] or None
            return row["value"], chat_id

    def set_chat_id(self, cookie: str, chat_id: str) -> None:
        """将 chat_id 绑定到指定 cookie。"""
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE cookies SET chat_id = ? WHERE value = ?",
                (chat_id, cookie),
            )
            conn.commit()

    def get_chat_id(self, cookie: str) -> str | None:
        """查询指定 cookie 当前绑定的 chat_id，无绑定返回 None。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT chat_id FROM cookies WHERE value = ?", (cookie,)
            ).fetchone()
        if row:
            return row["chat_id"] or None
        return None

    def clear_chat_id(self, cookie: str) -> None:
        """清除指定 cookie 绑定的 chat_id（chat 失效时调用）。"""
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE cookies SET chat_id = '' WHERE value = ?",
                (cookie,),
            )
            conn.commit()

    def mark_failed(self, cookie: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE cookies "
                "SET fail_count = fail_count + 1, "
                "    is_active = CASE WHEN fail_count + 1 >= ? THEN 0 ELSE is_active END "
                "WHERE value = ?",
                (COOKIE_MAX_FAIL_COUNT, cookie),
            )
            conn.commit()

    def mark_success(self, cookie: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE cookies SET fail_count = 0, is_active = 1 WHERE value = ?",
                (cookie,),
            )
            conn.commit()

    def list_all(self) -> list[CookieRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, value, is_active, fail_count, last_used, created_at, chat_id "
                "FROM cookies ORDER BY id"
            ).fetchall()
        return [
            CookieRecord(
                id=r["id"],
                value=r["value"],
                is_active=bool(r["is_active"]),
                fail_count=r["fail_count"],
                last_used=r["last_used"],
                created_at=r["created_at"],
                chat_id=r["chat_id"] or "",
            )
            for r in rows
        ]

    def size(self) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM cookies WHERE is_active = 1"
            ).fetchone()
        return row[0] if row else 0


# ─────────────────────────────────────────────────
# Redis 实现
# ─────────────────────────────────────────────────

class RedisStore:
    """基于 Redis 的 Cookie 存储，使用 LMOVE 实现原子 round-robin。

    数据结构：
      cookies:active   — List，存放活跃 Cookie（LMOVE LEFT RIGHT 实现轮询）
      cookies:meta     — Hash，field=cookie value=JSON{fail_count, created_at}
      cookies:inactive — Set，存放已失活 Cookie
    """

    _ACTIVE_KEY = "nexos:cookies:active"
    _META_KEY = "nexos:cookies:meta"
    _INACTIVE_KEY = "nexos:cookies:inactive"

    def __init__(self, url: str = REDIS_URL) -> None:
        try:
            import redis  # type: ignore
        except ImportError as exc:
            raise ImportError(
                "Redis 后端需要安装 redis 包：pip install redis"
            ) from exc
        self._r = redis.from_url(url, decode_responses=True)
        # 连接验证
        self._r.ping()

    def add_cookies(self, cookies: list[str]) -> int:
        import json
        added = 0
        pipe = self._r.pipeline()
        for cookie in cookies:
            cookie = cookie.strip()
            if not cookie:
                continue
            # 若 meta 中已存在则跳过（HSETNX：仅在字段不存在时设置）
            meta = json.dumps({"fail_count": 0, "created_at": time.time()})
            if self._r.hsetnx(self._META_KEY, cookie, meta):
                pipe.rpush(self._ACTIVE_KEY, cookie)
                added += 1
        pipe.execute()
        return added

    def get_next(self) -> str:
        # LMOVE LEFT RIGHT：原子地将队头移到队尾并返回，实现 round-robin
        cookie = self._r.lmove(self._ACTIVE_KEY, self._ACTIVE_KEY, "LEFT", "RIGHT")
        if cookie is None:
            raise RuntimeError(
                "Cookie 池为空或全部失效，请添加有效 Cookie。"
            )
        return cookie

    def mark_failed(self, cookie: str) -> None:
        import json
        raw = self._r.hget(self._META_KEY, cookie)
        if raw is None:
            return
        meta = json.loads(raw)
        meta["fail_count"] = meta.get("fail_count", 0) + 1
        self._r.hset(self._META_KEY, cookie, json.dumps(meta))
        if meta["fail_count"] >= COOKIE_MAX_FAIL_COUNT:
            # 从活跃列表移除，加入失活集合
            self._r.lrem(self._ACTIVE_KEY, 0, cookie)
            self._r.sadd(self._INACTIVE_KEY, cookie)

    def mark_success(self, cookie: str) -> None:
        import json
        raw = self._r.hget(self._META_KEY, cookie)
        meta = json.loads(raw) if raw else {}
        meta["fail_count"] = 0
        self._r.hset(self._META_KEY, cookie, json.dumps(meta))
        # 若在失活集合中，重新激活
        if self._r.sismember(self._INACTIVE_KEY, cookie):
            self._r.srem(self._INACTIVE_KEY, cookie)
            self._r.rpush(self._ACTIVE_KEY, cookie)

    def list_all(self) -> list[CookieRecord]:
        import json
        result: list[CookieRecord] = []
        active_set = set(self._r.lrange(self._ACTIVE_KEY, 0, -1))
        all_meta = self._r.hgetall(self._META_KEY)
        for i, (cookie, raw) in enumerate(all_meta.items(), start=1):
            meta = json.loads(raw)
            result.append(CookieRecord(
                id=i,
                value=cookie,
                is_active=cookie in active_set,
                fail_count=meta.get("fail_count", 0),
                last_used=None,
                created_at=meta.get("created_at", 0.0),
            ))
        return result

    def get_next_with_chat(self) -> tuple[str, str | None]:
        import json
        cookie = self._r.lmove(self._ACTIVE_KEY, self._ACTIVE_KEY, "LEFT", "RIGHT")
        if cookie is None:
            raise RuntimeError("Cookie 池为空或全部失效，请添加有效 Cookie。")
        raw = self._r.hget(self._META_KEY, cookie)
        chat_id: str | None = None
        if raw:
            meta = json.loads(raw)
            chat_id = meta.get("chat_id") or None
        return cookie, chat_id

    def set_chat_id(self, cookie: str, chat_id: str) -> None:
        import json
        raw = self._r.hget(self._META_KEY, cookie)
        meta = json.loads(raw) if raw else {}
        meta["chat_id"] = chat_id
        self._r.hset(self._META_KEY, cookie, json.dumps(meta))

    def get_chat_id(self, cookie: str) -> str | None:
        import json
        raw = self._r.hget(self._META_KEY, cookie)
        if not raw:
            return None
        return json.loads(raw).get("chat_id") or None

    def clear_chat_id(self, cookie: str) -> None:
        import json
        raw = self._r.hget(self._META_KEY, cookie)
        if not raw:
            return
        meta = json.loads(raw)
        meta.pop("chat_id", None)
        self._r.hset(self._META_KEY, cookie, json.dumps(meta))

    def size(self) -> int:
        return self._r.llen(self._ACTIVE_KEY)


# ─────────────────────────────────────────────────
# 工厂函数：根据配置创建存储实例
# ─────────────────────────────────────────────────

def create_store() -> SQLiteStore | RedisStore:
    """根据 COOKIE_STORE_BACKEND 环境变量创建对应存储实例。"""
    if COOKIE_STORE_BACKEND == "redis":
        return RedisStore(REDIS_URL)
    return SQLiteStore(COOKIE_DB_PATH)
