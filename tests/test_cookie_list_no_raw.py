"""P1: GET /admin/cookies 不应在列表接口中暴露 cookie 明文（_raw 字段）。"""
from unittest.mock import patch
from fastapi.testclient import TestClient
from dataclasses import dataclass, field
from typing import Optional
import time


@dataclass
class _FakeRecord:
    id: int = 1
    value: str = "secret-cookie-value-12345"
    is_active: bool = True
    fail_count: int = 0
    last_used: Optional[float] = None
    created_at: float = field(default_factory=time.time)
    last_success_at: Optional[float] = None
    total_requests: int = 0
    success_count: int = 0
    cooldown_until: float = 0.0
    label: str = ""
    priority: int = 0


def test_list_cookies_no_raw_field():
    """GET /admin/cookies 的每条 cookie 记录不得包含 _raw 明文字段。"""
    from main import app
    client = TestClient(app)
    mock_records = [_FakeRecord()]
    with patch("app.cookie_pool.list_all", return_value=mock_records):
        resp = client.get("/admin/cookies")
    assert resp.status_code == 200, f"期望 200，实际 {resp.status_code}: {resp.text}"
    cookies = resp.json()["cookies"]
    for c in cookies:
        assert "_raw" not in c, "_raw 字段不应暴露在 list 接口中"
        assert "secret-cookie-value-12345" not in str(c), "cookie 明文不应出现在 list 响应中"
