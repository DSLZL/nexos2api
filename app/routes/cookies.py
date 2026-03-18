"""Cookie 池管理接口（非 OpenAI 标准，内部管理用）。

GET  /admin              — 管理前端 UI
GET  /admin/cookies      — 列出所有 Cookie 状态
POST /admin/cookies      — 批量添加 Cookie
POST /admin/cookies/reload — 重新从环境变量导入
"""
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app import cookie_pool

router = APIRouter(tags=["cookie-management"])

_TEMPLATES_DIR = Path(__file__).parent.parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


class AddCookiesRequest(BaseModel):
    cookies: list[str]


@router.get("/admin", response_class=HTMLResponse, include_in_schema=False)
def admin_ui(request: Request):
    """渲染 Cookie 管理前端页面。"""
    return templates.TemplateResponse("admin.html", {"request": request})


@router.get("/admin/cookies")
def list_cookies():
    """列出所有 Cookie 及其健康状态。"""
    records = cookie_pool.list_all()
    return {
        "total": len(records),
        "active": sum(1 for r in records if r.is_active),
        "cookies": [
            {
                "id": r.id,
                "value": r.value[:20] + "..." if len(r.value) > 20 else r.value,
                "is_active": r.is_active,
                "fail_count": r.fail_count,
                "last_used": r.last_used,
                "created_at": r.created_at,
            }
            for r in records
        ],
    }


@router.post("/admin/cookies", status_code=201)
def add_cookies(body: AddCookiesRequest):
    """批量添加 Cookie（已存在的自动跳过）。"""
    added = cookie_pool.add_cookies(body.cookies)
    return {"added": added, "active_total": cookie_pool.size()}


@router.post("/admin/cookies/reload")
def reload_cookies():
    """重新从环境变量导入 Cookie。"""
    cookie_pool.reload()
    return {"active_total": cookie_pool.size()}
