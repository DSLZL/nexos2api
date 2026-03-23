"""Medium: 批量导入接口应限制单次最大条数，防止 DoS。"""
from unittest.mock import patch
from fastapi.testclient import TestClient


def test_batch_import_over_limit_rejected():
    """超过上限（500条）的批量导入请求应返回 422。"""
    from main import app
    client = TestClient(app)
    big_list = [f"cookie-value-{i}" for i in range(501)]
    with patch("app.cookie_pool.add_cookies", return_value=0):
        resp = client.post("/admin/cookies/batch", json={"cookies": big_list})
    assert resp.status_code in (400, 422), f"期望 400/422，实际 {resp.status_code}: {resp.text}"


def test_batch_import_within_limit_allowed():
    """不超过上限的请求应正常处理（返回 201）。"""
    from main import app
    client = TestClient(app)
    normal_list = [f"cookie-value-{i}" for i in range(10)]
    with patch("app.cookie_pool.add_cookies", return_value=10), \
         patch("app.cookie_pool.size", return_value=10), \
         patch("app.cookie_pool.list_all", return_value=[]), \
         patch("app.cookie_pool.update_cookie", return_value=True):
        resp = client.post("/admin/cookies/batch", json={"cookies": normal_list})
    assert resp.status_code == 201, f"期望 201，实际 {resp.status_code}: {resp.text}"
