"""统计数据 API 路由。

GET  /api/stats/overview   — 概览统计（今日请求、成功率、Cookie 状态）
GET  /api/stats/timeline   — 最近 24 小时按小时聚合
GET  /api/stats/requests   — 最近 N 条请求日志
GET  /api/stats/live       — SSE 实时推送（每 5 秒一次 overview）
"""
import asyncio
import json
import time

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from app import cookie_pool
from app.auth import require_admin
from app.model_registry import get_cached_model_count
from app.request_logger import get_logger

router = APIRouter(prefix="/api/stats", tags=["stats"], dependencies=[Depends(require_admin)])

_START_TIME = time.time()


def _build_overview() -> dict:
    """组装概览数据（cookie 状态 + 请求统计 + 运行时间 + 模型数）。"""
    records = cookie_pool.list_all()
    now = time.time()
    active = sum(1 for r in records if r.is_active and (r.cooldown_until is None or r.cooldown_until <= now))
    cooldown = sum(1 for r in records if r.is_active and r.cooldown_until and r.cooldown_until > now)
    disabled = sum(1 for r in records if not r.is_active)

    req_stats = get_logger().overview()
    uptime_s = int(now - _START_TIME)

    return {
        "cookies": {
            "total": len(records),
            "active": active,
            "cooldown": cooldown,
            "disabled": disabled,
        },
        "requests": req_stats,
        "uptime_seconds": uptime_s,
        "current_model_count": get_cached_model_count(),
    }


@router.get("/overview")
def stats_overview():
    """概览统计快照。"""
    return _build_overview()


@router.get("/timeline")
def stats_timeline():
    """最近 24 小时按整点小时聚合的请求量。"""
    return {"timeline": get_logger().timeline()}


@router.get("/requests")
def stats_requests(limit: int = 20):
    """最近 N 条请求日志（默认 20，最多 200）。"""
    limit = min(limit, 200)
    logs = get_logger().recent(limit)
    return {
        "logs": [
            {
                "id": r.id,
                "created_at": r.created_at,
                "cookie_hint": r.cookie_hint,
                "model": r.model,
                "success": r.success,
                "duration_ms": r.duration_ms,
                "error_msg": r.error_msg,
            }
            for r in logs
        ]
    }


@router.get("/live")
async def stats_live(request: Request):
    """SSE 端点：每 5 秒推送一次 overview 快照。"""

    async def _generate():
        while True:
            if await request.is_disconnected():
                break
            data = json.dumps(_build_overview(), ensure_ascii=False)
            yield f"data: {data}\n\n"
            await asyncio.sleep(5)

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
