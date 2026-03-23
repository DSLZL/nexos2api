"""P0: _check_one 应正确判断 cookie 健康状态，不能因解包错误将有效 cookie 误判为失败。"""
import asyncio
from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_check_one_returns_true_for_valid_cookie():
    """get_model_mapping 返回非空 dict 时，_check_one 应返回 True。"""
    from app.cookie_health import _check_one

    non_empty_mapping = {"claude-3-5-sonnet": "handler-abc", "gpt-4o": "handler-xyz"}
    with patch("app.model_registry.get_model_mapping", new=AsyncMock(return_value=non_empty_mapping)):
        # 需要确保锁不是 locked 状态，直接清空锁字典
        import app.cookie_health as ch
        ch._health_locks.clear()
        result = await _check_one("test-cookie-value-12345")
    assert result is True, f"非空 mapping 应返回 True，实际 {result!r}"


@pytest.mark.asyncio
async def test_check_one_returns_false_for_empty_mapping():
    """get_model_mapping 返回空 dict 时（cookie 无效），_check_one 应返回 False。"""
    from app.cookie_health import _check_one

    with patch("app.model_registry.get_model_mapping", new=AsyncMock(return_value={})):
        import app.cookie_health as ch
        ch._health_locks.clear()
        result = await _check_one("test-cookie-value-12345")
    assert result is False, f"空 mapping 应返回 False，实际 {result!r}"


@pytest.mark.asyncio
async def test_check_one_returns_false_on_exception():
    """get_model_mapping 抛出异常时，_check_one 应返回 False 而非传播异常。"""
    from app.cookie_health import _check_one

    with patch("app.model_registry.get_model_mapping", new=AsyncMock(side_effect=RuntimeError("network error"))):
        import app.cookie_health as ch
        ch._health_locks.clear()
        result = await _check_one("test-cookie-value-12345")
    assert result is False


@pytest.mark.asyncio
async def test_check_one_skips_when_locked():
    """锁已占用时（另一次检查进行中）应乐观返回 True，不阻塞。"""
    from app.cookie_health import _check_one, _get_lock
    import app.cookie_health as ch
    ch._health_locks.clear()

    cookie = "test-cookie-locked-67890"
    lock = _get_lock(cookie)

    async def _hold_lock():
        async with lock:
            # 锁被占用期间调用 _check_one
            result = await _check_one(cookie)
            assert result is True, "锁占用时应乐观返回 True"

    await _hold_lock()
