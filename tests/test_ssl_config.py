"""P1: make_client() 的 SSL 验证行为应可通过 NEXOS_VERIFY_SSL 环境变量控制。"""
from unittest.mock import patch, MagicMock
import os


def test_ssl_verify_true_by_default():
    """未设置 NEXOS_VERIFY_SSL 时，默认应为 True（安全默认值）。"""
    with patch.dict(os.environ, {}, clear=False):
        # 移除可能存在的覆盖
        os.environ.pop("NEXOS_VERIFY_SSL", None)
        import importlib
        import app.config as cfg
        importlib.reload(cfg)
        assert cfg.NEXOS_VERIFY_SSL is True, "默认 SSL 验证应为 True"


def test_ssl_verify_controllable_via_env():
    """设置 NEXOS_VERIFY_SSL=true 时，make_client() 应传入 verify=True。"""
    captured = {}

    original_init = __import__("httpx").AsyncClient.__init__

    def mock_init(self, **kwargs):
        captured["verify"] = kwargs.get("verify")
        # 用最小配置初始化，避免真实网络连接
        original_init(self, verify=kwargs.get("verify", True))

    with patch.dict(os.environ, {"NEXOS_VERIFY_SSL": "true"}):
        import importlib
        import app.config as cfg
        importlib.reload(cfg)
        import app.nexos_client as nc
        importlib.reload(nc)
        with patch("httpx.AsyncClient.__init__", mock_init):
            nc.make_client()
    assert captured.get("verify") is True, f"期望 verify=True，实际 {captured.get('verify')}"


def test_ssl_verify_false_when_disabled():
    """设置 NEXOS_VERIFY_SSL=false 时，make_client() 应传入 verify=False。"""
    captured = {}

    original_init = __import__("httpx").AsyncClient.__init__

    def mock_init(self, **kwargs):
        captured["verify"] = kwargs.get("verify")
        original_init(self, verify=kwargs.get("verify", True))

    with patch.dict(os.environ, {"NEXOS_VERIFY_SSL": "false"}):
        import importlib
        import app.config as cfg
        importlib.reload(cfg)
        import app.nexos_client as nc
        importlib.reload(nc)
        with patch("httpx.AsyncClient.__init__", mock_init):
            nc.make_client()
    assert captured.get("verify") is False, f"期望 verify=False，实际 {captured.get('verify')}"
