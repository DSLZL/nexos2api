"""P0: /admin UI 页面和 /api/admin/* 接口在设置 ADMIN_PASSWORD 时必须要求鉴权。"""
import os
from unittest.mock import patch
from fastapi.testclient import TestClient


def test_admin_ui_requires_auth_when_password_set():
    """配置 ADMIN_PASSWORD 后，GET /admin 不带 token 应返回 401。"""
    with patch.dict(os.environ, {"ADMIN_PASSWORD": "test-secret"}):
        import importlib
        import app.config as cfg
        importlib.reload(cfg)
        import app.auth as auth
        importlib.reload(auth)
        from main import app
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/admin")
    assert resp.status_code == 401, f"期望 401，实际 {resp.status_code}"


def test_admin_ui_accessible_with_correct_token():
    """提供正确 Bearer token 时 GET /admin 应返回 200。"""
    with patch.dict(os.environ, {"ADMIN_PASSWORD": "test-secret"}):
        import importlib
        import app.config as cfg
        importlib.reload(cfg)
        import app.auth as auth
        importlib.reload(auth)
        from main import app
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/admin", headers={"Authorization": "Bearer test-secret"})
    assert resp.status_code == 200


def test_admin_ui_accessible_without_password_configured():
    """未配置 ADMIN_PASSWORD 时，GET /admin 无 token 应直接放行（200）。"""
    with patch.dict(os.environ, {"ADMIN_PASSWORD": ""}):
        import importlib
        import app.config as cfg
        importlib.reload(cfg)
        import app.auth as auth
        importlib.reload(auth)
        from main import app
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/admin")
    assert resp.status_code == 200
