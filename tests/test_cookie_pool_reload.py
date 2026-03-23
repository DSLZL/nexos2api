"""P0: cookie_pool.reload() 必须存在且可调用。"""
import importlib
import sys


def test_reload_exists():
    """cookie_pool 模块必须暴露 reload() 函数。"""
    import app.cookie_pool as pool
    assert callable(getattr(pool, "reload", None)), "cookie_pool 缺少 reload() 函数"


def test_reload_runs_without_error():
    """reload() 调用后不应抛出异常。"""
    import app.cookie_pool as pool
    pool.reload()  # 不应抛出 AttributeError 或其他异常
