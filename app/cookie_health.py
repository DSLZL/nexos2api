"""后台 Cookie 健康检查服务。

周期性验证每个活跃 Cookie 是否仍能访问 Nexos API，
失败则调用 mark_failed，达阈值自动停用；
COOKIE_AUTO_RECOVER=true 时会定期重试已停用 Cookie。

配置项（.env）：
  COOKIE_HEALTH_CHECK_INTERVAL   # 检查间隔（秒），默认 300
  COOKIE_AUTO_RECOVER            # 是否自动恢复停用 Cookie，默认 false
"""
from __future__ import annotations

import asyncio
import time
from typing import Optional

from app.config import COOKIE_HEALTH_CHECK_INTERVAL, COOKIE_AUTO_RECOVER
from app import cookie_pool

# per-cookie 异步锁，与 routes/chat.py 中的 _cookie_locks 分离，
# 健康检查使用独立锁集避免影响正常请求吞吐。
_health_locks: dict[str, asyncio.Lock] = {}


def _get_lock(cookie: str) -> asyncio.Lock:
    key = cookie[:32]
    if key not in _health_locks:
        _health_locks[key] = asyncio.Lock()
    return _health_locks[key]


async def _check_one(cookie: str) -> bool:
    """验证单个 Cookie 是否有效：调用 model_registry 获取模型列表。

    返回 True 表示健康，False 表示失败。
    """
    from app.model_registry import get_model_mapping  # 避免循环导入

    lock = _get_lock(cookie)
    if lock.locked():
        # 该 Cookie 正在被健康检查，跳过本轮
        return True

    async with lock:
        try:
            mapping = await get_model_mapping(cookie)
            return bool(mapping)  # 非空映射 => 有效
        except Exception:
            return False


async def _run_check(cookie: str, is_recovery: bool = False) -> None:
    """执行一次检查并更新统计和健康状态。"""
    healthy = await _check_one(cookie)
    cookie_pool.record_health(cookie, healthy)
    if healthy:
        cookie_pool.mark_success(cookie)
        if is_recovery:
            print(f"[health] Cookie ...{cookie[-8:]} 恢复正常")
    else:
        cookie_pool.mark_failed(cookie)
        print(f"[health] Cookie ...{cookie[-8:]} 健康检查失败")


async def health_check_loop() -> None:
    """后台循环：每 COOKIE_HEALTH_CHECK_INTERVAL 秒执行一轮全量检查。"""
    print(f"[health] 健康检查服务启动，间隔 {COOKIE_HEALTH_CHECK_INTERVAL}s"
          + ("，自动恢复已启用" if COOKIE_AUTO_RECOVER else ""))

    # 启动时立即执行一轮
    await _full_check()

    while True:
        await asyncio.sleep(COOKIE_HEALTH_CHECK_INTERVAL)
        await _full_check()


async def _full_check() -> None:
    """对所有需检查的 Cookie 并发执行健康检查。"""
    records = cookie_pool.list_all()
    tasks: list[asyncio.Task] = []

    for r in records:
        if r.is_active:
            # 对活跃 Cookie 做健康检查
            tasks.append(asyncio.create_task(_run_check(r.value)))
        elif COOKIE_AUTO_RECOVER:
            # 对停用 Cookie 尝试恢复
            tasks.append(asyncio.create_task(_run_check(r.value, is_recovery=True)))

    if not tasks:
        return

    results = await asyncio.gather(*tasks, return_exceptions=True)
    errors = [r for r in results if isinstance(r, Exception)]
    if errors:
        print(f"[health] 本轮检查有 {len(errors)} 个异常")


async def trigger_check_one(cookie: str) -> dict:
    """立即触发单个 Cookie 健康检查，返回检查结果。供 API 端点调用。"""
    t0 = time.time()
    healthy = await _check_one(cookie)
    elapsed_ms = int((time.time() - t0) * 1000)
    if not healthy:
        cookie_pool.mark_failed(cookie)
    else:
        cookie_pool.mark_success(cookie)
    return {
        "healthy": healthy,
        "elapsed_ms": elapsed_ms,
    }


async def trigger_check_all() -> dict:
    """立即触发全量健康检查，返回汇总结果。供 API 端点调用。"""
    t0 = time.time()
    records = cookie_pool.list_all()
    tasks = [asyncio.create_task(_run_check(r.value)) for r in records]
    if not tasks:
        return {"checked": 0, "elapsed_ms": 0}
    await asyncio.gather(*tasks, return_exceptions=True)
    return {
        "checked": len(tasks),
        "elapsed_ms": int((time.time() - t0) * 1000),
    }
