"""请求日志模块 — 记录每次 /v1/chat/completions 调用结果。

存储后端：SQLite（复用 COOKIE_DB_PATH 同目录的 logs.db）。
内存中维护最近 200 条 ring buffer，供 SSE 实时推送。

配置：
  ENABLE_REQUEST_LOGGING=true   启用日志（默认开）
  LOG_RETENTION_DAYS=7          日志保留天数
"""
from __future__ import annotations

import sqlite3
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from app.config import COOKIE_DB_PATH, ENABLE_REQUEST_LOGGING, LOG_RETENTION_DAYS

# logs.db 与 cookies.db 同目录
_LOG_DB_PATH = str(Path(COOKIE_DB_PATH).parent / "logs.db")
_RING_SIZE = 200  # 内存 ring buffer 容量
_CLEANUP_INTERVAL = 3600  # 定期清理间隔（秒），默认每小时一次

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS request_logs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at   REAL NOT NULL,
    cookie_id    INTEGER,
    cookie_hint  TEXT,
    model        TEXT,
    success      INTEGER NOT NULL,
    duration_ms  INTEGER,
    error_msg    TEXT
)
"""
_CREATE_INDEX = "CREATE INDEX IF NOT EXISTS idx_rl_created ON request_logs(created_at)"


@dataclass
class RequestLog:
    id: int
    created_at: float
    cookie_id: Optional[int]
    cookie_hint: str       # cookie 前 8 位...后 4 位，脱敏
    model: str
    success: bool
    duration_ms: Optional[int]
    error_msg: str


class RequestLogger:
    """线程安全的请求日志记录器。"""

    def __init__(self, db_path: str = _LOG_DB_PATH) -> None:
        self._db_path = db_path
        self._lock = threading.Lock()
        self._ring: deque[RequestLog] = deque(maxlen=_RING_SIZE)
        self._last_cleanup_ts: float = time.time()
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
            conn.execute(_CREATE_INDEX)
            conn.commit()

    def _cleanup_old(self, conn: sqlite3.Connection) -> None:
        cutoff = time.time() - LOG_RETENTION_DAYS * 86400
        conn.execute("DELETE FROM request_logs WHERE created_at < ?", (cutoff,))

    @staticmethod
    def _hint(cookie: str) -> str:
        if len(cookie) <= 12:
            return cookie[:4] + "..."
        return cookie[:8] + "..." + cookie[-4:]

    def record(
        self,
        cookie: str,
        model: str,
        success: bool,
        duration_ms: Optional[int] = None,
        error_msg: str = "",
        cookie_id: Optional[int] = None,
    ) -> None:
        """写入一条日志（同步，加锁）。"""
        if not ENABLE_REQUEST_LOGGING:
            return
        entry = RequestLog(
            id=0,
            created_at=time.time(),
            cookie_id=cookie_id,
            cookie_hint=self._hint(cookie),
            model=model,
            success=success,
            duration_ms=duration_ms,
            error_msg=error_msg,
        )
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO request_logs "
                "(created_at, cookie_id, cookie_hint, model, success, duration_ms, error_msg) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    entry.created_at,
                    entry.cookie_id,
                    entry.cookie_hint,
                    entry.model,
                    int(entry.success),
                    entry.duration_ms,
                    entry.error_msg,
                ),
            )
            entry.id = cur.lastrowid or 0
            _now = time.time()
            if _now - self._last_cleanup_ts >= _CLEANUP_INTERVAL:
                self._cleanup_old(conn)
                self._last_cleanup_ts = _now
                # 清理后收缩 WAL 文件，归还磁盘空间
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.commit()
        self._ring.append(entry)

    def recent(self, limit: int = 20) -> list[RequestLog]:
        """返回内存 ring buffer 中最近 N 条（最新在前）。"""
        return list(reversed(list(self._ring)))[:limit]

    def overview(self) -> dict:
        """返回今日请求统计概览。"""
        today_start = time.time() - 86400  # 最近 24 小时
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) as total, "
                "SUM(CASE WHEN success=1 THEN 1 ELSE 0 END) as ok, "
                "AVG(CASE WHEN duration_ms IS NOT NULL THEN duration_ms END) as avg_ms "
                "FROM request_logs WHERE created_at >= ?",
                (today_start,),
            ).fetchone()
        total = row["total"] or 0
        ok = row["ok"] or 0
        avg_ms = int(row["avg_ms"]) if row["avg_ms"] else None
        return {
            "total_24h": total,
            "success_24h": ok,
            "failure_24h": total - ok,
            "success_rate": round(ok / total * 100, 1) if total else None,
            "avg_duration_ms": avg_ms,
        }

    def timeline(self) -> list[dict]:
        """返回最近 24 小时按整点小时聚合的统计。"""
        cutoff = time.time() - 86400
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT "
                "  CAST(created_at / 3600 AS INTEGER) * 3600 AS hour_ts, "
                "  COUNT(*) AS total, "
                "  SUM(CASE WHEN success=1 THEN 1 ELSE 0 END) AS ok "
                "FROM request_logs "
                "WHERE created_at >= ? "
                "GROUP BY hour_ts "
                "ORDER BY hour_ts",
                (cutoff,),
            ).fetchall()
        return [
            {
                "hour_ts": r["hour_ts"],
                "requests": r["total"],
                "successes": r["ok"],
                "failures": r["total"] - r["ok"],
            }
            for r in rows
        ]


# 全局单例
_logger: Optional[RequestLogger] = None


def get_logger() -> RequestLogger:
    global _logger
    if _logger is None:
        _logger = RequestLogger()
    return _logger
