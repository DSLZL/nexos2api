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
    COOKIE_ROTATION_STRATEGY,
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
    last_success_at: Optional[float] = None
    total_requests: int = 0
    success_count: int = 0
    cooldown_until: Optional[float] = None
    label: str = ""
    priority: int = 0
    last_health_check_at: Optional[float] = None
    health_status: str = "unknown"  # unknown | healthy | unhealthy


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

    def update_cookie(self, cookie: str, label: str | None, priority: int | None, is_active: bool | None) -> bool: ...
    """更新 Cookie 元信息（label/priority/is_active），返回是否找到记录。"""

    def delete_cookie(self, cookie: str) -> bool: ...

    def record_health(self, cookie: str, healthy: bool) -> None: ...
    """从存储中永久删除指定 Cookie，返回是否找到记录。"""


# ─────────────────────────────────────────────────
# SQLite 实现
# ─────────────────────────────────────────────────

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS cookies (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    value                 TEXT UNIQUE NOT NULL,
    is_active             INTEGER DEFAULT 1,
    fail_count            INTEGER DEFAULT 0,
    last_used             REAL,
    created_at            REAL NOT NULL,
    chat_id               TEXT DEFAULT '',
    last_success_at       REAL,
    total_requests        INTEGER DEFAULT 0,
    success_count         INTEGER DEFAULT 0,
    cooldown_until        REAL,
    label                 TEXT DEFAULT '',
    priority              INTEGER DEFAULT 0,
    last_health_check_at  REAL,
    health_status         TEXT DEFAULT 'unknown'
)
"""

# 旧表迁移：按列名逐一检测补全
_MIGRATIONS: list[str] = [
    "ALTER TABLE cookies ADD COLUMN chat_id TEXT DEFAULT ''",
    "ALTER TABLE cookies ADD COLUMN last_success_at REAL",
    "ALTER TABLE cookies ADD COLUMN total_requests INTEGER DEFAULT 0",
    "ALTER TABLE cookies ADD COLUMN success_count INTEGER DEFAULT 0",
    "ALTER TABLE cookies ADD COLUMN cooldown_until REAL",
    "ALTER TABLE cookies ADD COLUMN label TEXT DEFAULT ''",
    "ALTER TABLE cookies ADD COLUMN priority INTEGER DEFAULT 0",
    "ALTER TABLE cookies ADD COLUMN last_health_check_at REAL",
    "ALTER TABLE cookies ADD COLUMN health_status TEXT DEFAULT 'unknown'",
]


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
            cols = {row[1] for row in conn.execute("PRAGMA table_info(cookies)")}
            for stmt in _MIGRATIONS:
                # 从 ALTER TABLE ADD COLUMN 语句中提取列名
                col_name = stmt.split("ADD COLUMN ")[1].split()[0]
                if col_name not in cols:
                    conn.execute(stmt)
            conn.commit()

    def add_cookies(self, cookies: list[str]) -> int:
        added = 0
        with self._lock, self._connect() as conn:
            for cookie in cookies:
                # 与 _load_from_env 保持一致：移除所有换行符再 strip
                cookie = cookie.replace("\r", "").replace("\n", "").strip()
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
        """按 COOKIE_ROTATION_STRATEGY 选取下一个可用 Cookie。

        - weighted   (默认): priority DESC, last_used ASC
        - round-robin:       纯 last_used ASC（忽略 priority）
        - least-used:        total_requests ASC, last_used ASC
        """
        if COOKIE_ROTATION_STRATEGY == "round-robin":
            order = "ORDER BY last_used ASC NULLS FIRST"
        elif COOKIE_ROTATION_STRATEGY == "least-used":
            order = "ORDER BY total_requests ASC, last_used ASC NULLS FIRST"
        else:  # weighted（默认）
            order = "ORDER BY priority DESC, last_used ASC NULLS FIRST"

        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT id, value, chat_id FROM cookies "
                "WHERE is_active = 1 "
                "  AND (cooldown_until IS NULL OR cooldown_until <= strftime('%s','now')) "
                + order +
                " LIMIT 1"
            ).fetchone()
            if row is None:
                raise RuntimeError(
                    "Cookie 池为空或全部失效/冷却中，请添加有效 Cookie。"
                )
            conn.execute(
                "UPDATE cookies SET last_used = ?, total_requests = total_requests + 1 WHERE id = ?",
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
        """标记失败：累计超阈值停用；否则设置冷却到期时间。"""
        from app.config import COOKIE_COOLDOWN_SECONDS
        with self._lock, self._connect() as conn:
            cooldown_until = time.time() + COOKIE_COOLDOWN_SECONDS if COOKIE_COOLDOWN_SECONDS > 0 else None
            conn.execute(
                "UPDATE cookies "
                "SET fail_count = fail_count + 1, "
                "    is_active = CASE WHEN fail_count + 1 >= ? THEN 0 ELSE is_active END, "
                "    cooldown_until = CASE WHEN fail_count + 1 >= ? THEN NULL ELSE ? END "
                "WHERE value = ?",
                (COOKIE_MAX_FAIL_COUNT, COOKIE_MAX_FAIL_COUNT, cooldown_until, cookie),
            )
            conn.commit()

    def mark_success(self, cookie: str) -> None:
        """标记成功：重置失败计数、清除冷却，更新成功统计。"""
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE cookies "
                "SET fail_count = 0, is_active = 1, cooldown_until = NULL, "
                "    last_success_at = ?, success_count = success_count + 1 "
                "WHERE value = ?",
                (time.time(), cookie),
            )
            conn.commit()

    def list_all(self) -> list[CookieRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, value, is_active, fail_count, last_used, created_at, chat_id, "
                "       last_success_at, total_requests, success_count, cooldown_until, label, priority, "
                "       last_health_check_at, health_status "
                "FROM cookies ORDER BY priority DESC, id"
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
                last_success_at=r["last_success_at"],
                total_requests=r["total_requests"] or 0,
                success_count=r["success_count"] or 0,
                cooldown_until=r["cooldown_until"],
                label=r["label"] or "",
                priority=r["priority"] or 0,
                last_health_check_at=r["last_health_check_at"],
                health_status=r["health_status"] or "unknown",
            )
            for r in rows
        ]

    def size(self) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM cookies WHERE is_active = 1"
            ).fetchone()
        return row[0] if row else 0

    def update_cookie(self, cookie: str, label: str | None, priority: int | None, is_active: bool | None) -> bool:
        """更新 label / priority / is_active，跳过 None 参数。返回是否找到记录。"""
        parts: list[str] = []
        params: list = []
        if label is not None:
            parts.append("label = ?")
            params.append(label)
        if priority is not None:
            parts.append("priority = ?")
            params.append(priority)
        if is_active is not None:
            parts.append("is_active = ?")
            params.append(int(is_active))
        if not parts:
            return True  # 无需更新
        params.append(cookie)
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                f"UPDATE cookies SET {', '.join(parts)} WHERE value = ?",
                params,
            )
            conn.commit()
        return cur.rowcount > 0

    def delete_cookie(self, cookie: str) -> bool:
        """从存储中永久删除指定 Cookie。返回是否找到记录。"""
        with self._lock, self._connect() as conn:
            cur = conn.execute("DELETE FROM cookies WHERE value = ?", (cookie,))
            conn.commit()
        return cur.rowcount > 0

    def record_health(self, cookie: str, healthy: bool) -> None:
        """记录健康检查结果：更新 last_health_check_at 和 health_status。"""
        status = "healthy" if healthy else "unhealthy"
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE cookies SET last_health_check_at = ?, health_status = ? WHERE value = ?",
                (time.time(), status, cookie),
            )
            conn.commit()


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
            # 与 _load_from_env 保持一致：移除所有换行符再 strip
            cookie = cookie.replace("\r", "").replace("\n", "").strip()
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
        from app.config import COOKIE_COOLDOWN_SECONDS
        raw = self._r.hget(self._META_KEY, cookie)
        if raw is None:
            return
        meta = json.loads(raw)
        meta["fail_count"] = meta.get("fail_count", 0) + 1
        if meta["fail_count"] >= COOKIE_MAX_FAIL_COUNT:
            # 达到阈值：移除活跃列表，加入失活集合，清除冷却
            meta.pop("cooldown_until", None)
            self._r.hset(self._META_KEY, cookie, json.dumps(meta))
            self._r.lrem(self._ACTIVE_KEY, 0, cookie)
            self._r.sadd(self._INACTIVE_KEY, cookie)
        else:
            # 未达阈值：设置冷却时间
            if COOKIE_COOLDOWN_SECONDS > 0:
                meta["cooldown_until"] = time.time() + COOKIE_COOLDOWN_SECONDS
            self._r.hset(self._META_KEY, cookie, json.dumps(meta))

    def mark_success(self, cookie: str) -> None:
        import json
        raw = self._r.hget(self._META_KEY, cookie)
        meta = json.loads(raw) if raw else {}
        meta["fail_count"] = 0
        meta["last_success_at"] = time.time()
        meta["success_count"] = meta.get("success_count", 0) + 1
        meta.pop("cooldown_until", None)
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
            cooldown_until = meta.get("cooldown_until")
            result.append(CookieRecord(
                id=i,
                value=cookie,
                is_active=cookie in active_set,
                fail_count=meta.get("fail_count", 0),
                last_used=meta.get("last_used"),
                created_at=meta.get("created_at", 0.0),
                chat_id=meta.get("chat_id", ""),
                last_success_at=meta.get("last_success_at"),
                total_requests=meta.get("total_requests", 0),
                success_count=meta.get("success_count", 0),
                cooldown_until=cooldown_until,
                label=meta.get("label", ""),
                priority=meta.get("priority", 0),
                last_health_check_at=meta.get("last_health_check_at"),
                health_status=meta.get("health_status", "unknown"),
            ))
        return result

    def get_next_with_chat(self) -> tuple[str, str | None]:
        import json
        # 加权轮询：过滤冷却中，按 priority DESC / last_used ASC
        all_meta = self._r.hgetall(self._META_KEY)
        active_set = set(self._r.lrange(self._ACTIVE_KEY, 0, -1))
        now = time.time()
        candidates = []
        for cookie, raw in all_meta.items():
            if cookie not in active_set:
                continue
            meta = json.loads(raw)
            cooldown_until = meta.get("cooldown_until")
            if cooldown_until and cooldown_until > now:
                continue
            candidates.append((cookie, meta))
        if not candidates:
            raise RuntimeError("Cookie 池为空或全部失效/冷却中，请添加有效 Cookie。")
        # priority DESC, last_used ASC (None 视为最旧)
        candidates.sort(key=lambda x: (-x[1].get("priority", 0), x[1].get("last_used") or 0.0))
        cookie, meta = candidates[0]
        meta["last_used"] = now
        meta["total_requests"] = meta.get("total_requests", 0) + 1
        self._r.hset(self._META_KEY, cookie, json.dumps(meta))
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

    def update_cookie(self, cookie: str, label: str | None, priority: int | None, is_active: bool | None) -> bool:
        """更新 label / priority / is_active，跳过 None 参数。返回是否找到记录。"""
        import json
        raw = self._r.hget(self._META_KEY, cookie)
        if raw is None:
            return False
        meta = json.loads(raw)
        if label is not None:
            meta["label"] = label
        if priority is not None:
            meta["priority"] = priority
        if is_active is not None:
            active_set = set(self._r.lrange(self._ACTIVE_KEY, 0, -1))
            currently_active = cookie in active_set
            if is_active and not currently_active:
                # 从失活集合移到活跃列表
                self._r.srem(self._INACTIVE_KEY, cookie)
                self._r.rpush(self._ACTIVE_KEY, cookie)
            elif not is_active and currently_active:
                # 从活跃列表移到失活集合
                self._r.lrem(self._ACTIVE_KEY, 0, cookie)
                self._r.sadd(self._INACTIVE_KEY, cookie)
        self._r.hset(self._META_KEY, cookie, json.dumps(meta))
        return True

    def delete_cookie(self, cookie: str) -> bool:
        """从存储中永久删除指定 Cookie，返回是否找到记录。"""
        existed = self._r.hexists(self._META_KEY, cookie)
        if not existed:
            return False
        self._r.hdel(self._META_KEY, cookie)
        self._r.lrem(self._ACTIVE_KEY, 0, cookie)
        self._r.srem(self._INACTIVE_KEY, cookie)
        return True

    def record_health(self, cookie: str, healthy: bool) -> None:
        """记录健康检查结果到 Redis meta hash。"""
        import json
        raw = self._r.hget(self._META_KEY, cookie)
        if raw is None:
            return
        meta = json.loads(raw)
        meta["last_health_check_at"] = time.time()
        meta["health_status"] = "healthy" if healthy else "unhealthy"
        self._r.hset(self._META_KEY, cookie, json.dumps(meta))


# ─────────────────────────────────────────────────
# 工厂函数：根据配置创建存储实例
# ─────────────────────────────────────────────────

def create_store() -> SQLiteStore | RedisStore:
    """根据 COOKIE_STORE_BACKEND 环境变量创建对应存储实例。"""
    if COOKIE_STORE_BACKEND == "redis":
        return RedisStore(REDIS_URL)
    return SQLiteStore(COOKIE_DB_PATH)
