"""Cookie 池管理接口（非 OpenAI 标准，内部管理用）。

GET  /admin                        — 管理前端 UI
GET  /admin/cookies                — 列出所有 Cookie 状态
POST /admin/cookies                — 批量添加 Cookie
POST /admin/cookies/batch          — 批量导入（换行分隔文本）
PATCH /admin/cookies/{cookie_id}   — 更新 Cookie 元信息（label/priority/is_active）
DELETE /admin/cookies/{cookie_id}  — 删除指定 Cookie
DELETE /admin/cookies/batch        — 批量删除
POST /admin/cookies/{id}/enable    — 手动启用（重置 fail_count）
POST /admin/cookies/{id}/disable   — 手动停用
POST /admin/cookies/{id}/check     — 立即触发单个 Cookie 健康检查
POST /admin/cookies/check-all      — 立即触发全量健康检查
GET  /admin/cookies/export         — 导出所有 Cookie（JSON 下载）
POST /admin/cookies/reload         — 重新从环境变量导入
"""
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, model_validator

from app import cookie_pool
from app.auth import require_admin

router = APIRouter(tags=["cookie-management"])
# 需要认证的 API 子路由
_api = APIRouter(dependencies=[Depends(require_admin)])

_TEMPLATES_DIR = Path(__file__).parent.parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


class AddCookiesRequest(BaseModel):
    cookies: list[str]


class UpdateCookieRequest(BaseModel):
    label: Optional[str] = None
    priority: Optional[int] = None
    is_active: Optional[bool] = None


def _record_to_dict(r, *, include_raw: bool = False) -> dict:
    """将 CookieRecord 序列化为 API 响应字典。

    Args:
        include_raw: 为 True 时包含完整 cookie 原始值（仅供 export 接口使用）。
    """
    d = {
        "id": r.id,
        "value": r.value[:20] + "..." if len(r.value) > 20 else r.value,
        "is_active": r.is_active,
        "fail_count": r.fail_count,
        "last_used": r.last_used,
        "created_at": r.created_at,
        "last_success_at": r.last_success_at,
        "total_requests": r.total_requests,
        "success_count": r.success_count,
        "cooldown_until": r.cooldown_until,
        "label": r.label,
        "priority": r.priority,
    }
    if include_raw:
        d["_raw"] = r.value
    return d


@_api.get("/admin", response_class=HTMLResponse, include_in_schema=False)
def admin_ui(request: Request):
    """渲染 Cookie 管理前端页面。"""
    return templates.TemplateResponse(request, "admin.html")


@_api.get("/admin/cookies")
def list_cookies():
    """列出所有 Cookie 及其健康状态。"""
    records = cookie_pool.list_all()
    return {
        "total": len(records),
        "active": sum(1 for r in records if r.is_active),
        "cookies": [_record_to_dict(r) for r in records],
    }


@_api.post("/admin/cookies", status_code=201)
def add_cookies(body: AddCookiesRequest):
    """批量添加 Cookie（已存在的自动跳过）。"""
    added = cookie_pool.add_cookies(body.cookies)
    return {"added": added, "active_total": cookie_pool.size()}


class UpdateCookieByValueRequest(BaseModel):
    cookie: str
    label: Optional[str] = None
    priority: Optional[int] = None
    is_active: Optional[bool] = None


class DeleteCookieByValueRequest(BaseModel):
    cookie: str


@_api.patch("/admin/cookies")
def update_cookie_by_value(body: UpdateCookieByValueRequest):
    """按 cookie 原始值更新元信息（供前端使用）。"""
    ok = cookie_pool.update_cookie(
        cookie=body.cookie,
        label=body.label,
        priority=body.priority,
        is_active=body.is_active,
    )
    if not ok:
        raise HTTPException(status_code=404, detail="Cookie 不存在")
    return {"success": True, "active_total": cookie_pool.size()}


@_api.delete("/admin/cookies")
def delete_cookie_by_value(body: DeleteCookieByValueRequest):
    """按 cookie 原始值永久删除（供前端使用）。"""
    ok = cookie_pool.delete_cookie(body.cookie)
    if not ok:
        raise HTTPException(status_code=404, detail="Cookie 不存在")
    return {"success": True, "active_total": cookie_pool.size()}


@_api.patch("/admin/cookies/{cookie_id}")
def update_cookie(cookie_id: int, body: UpdateCookieRequest):
    """按 id 更新 Cookie 的 label / priority / is_active。"""
    records = cookie_pool.list_all()
    target = next((r for r in records if r.id == cookie_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail=f"Cookie id={cookie_id} 不存在")
    ok = cookie_pool.update_cookie(
        cookie=target.value,
        label=body.label,
        priority=body.priority,
        is_active=body.is_active,
    )
    if not ok:
        raise HTTPException(status_code=404, detail=f"Cookie id={cookie_id} 更新失败")
    return {"success": True, "active_total": cookie_pool.size()}


@_api.delete("/admin/cookies/{cookie_id}", status_code=200)
def delete_cookie(cookie_id: int):
    """按 id 永久删除指定 Cookie。"""
    records = cookie_pool.list_all()
    target = next((r for r in records if r.id == cookie_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail=f"Cookie id={cookie_id} 不存在")
    ok = cookie_pool.delete_cookie(target.value)
    if not ok:
        raise HTTPException(status_code=404, detail=f"Cookie id={cookie_id} 删除失败")
    return {"success": True, "active_total": cookie_pool.size()}


@_api.post("/admin/cookies/reload")
def reload_cookies():
    """重新从环境变量导入 Cookie。"""
    cookie_pool.reload()
    return {"active_total": cookie_pool.size()}


class BatchImportRequest(BaseModel):
    text: Optional[str] = None          # 换行分隔文本
    cookies: Optional[list[str]] = None  # JSON 数组
    label: Optional[str] = None          # 统一标签前缀（可选）

    @model_validator(mode="after")
    def check_total_count(self) -> "BatchImportRequest":
        """合并后 cookie 总数不得超过 500，防止 DoS。"""
        total = len(self.cookies or [])
        if self.text:
            total += sum(1 for line in self.text.splitlines() if line.strip())
        if total > 500:
            raise ValueError(f"单次最多导入 500 条，当前 {total} 条")
        return self


class BatchDeleteRequest(BaseModel):
    ids: list[int]


@_api.post("/admin/cookies/batch", status_code=201)
def batch_import_cookies(body: BatchImportRequest):
    """批量导入 Cookie（JSON 数组或换行分隔文本），可附带统一标签。"""
    raw_list: list[str] = []
    if body.cookies:
        raw_list.extend(body.cookies)
    if body.text:
        raw_list.extend(line.strip() for line in body.text.splitlines() if line.strip())
    # 去重
    seen: set[str] = set()
    deduped: list[str] = []
    for c in raw_list:
        if c not in seen:
            seen.add(c)
            deduped.append(c)
    added = cookie_pool.add_cookies(deduped)
    # 若指定统一标签，为刚导入的 Cookie 设置
    if body.label and added > 0:
        records = cookie_pool.list_all()
        for r in records:
            if r.value in seen and (r.label == "" or r.label is None):
                cookie_pool.update_cookie(r.value, label=body.label)
    return {"parsed": len(deduped), "added": added, "skipped": len(deduped) - added, "active_total": cookie_pool.size()}


@_api.delete("/admin/cookies/batch")
def batch_delete_cookies(body: BatchDeleteRequest):
    """批量删除指定 id 列表的 Cookie。"""
    records = cookie_pool.list_all()
    id_to_value = {r.id: r.value for r in records}
    deleted = 0
    not_found: list[int] = []
    for cid in body.ids:
        if cid not in id_to_value:
            not_found.append(cid)
            continue
        if cookie_pool.delete_cookie(id_to_value[cid]):
            deleted += 1
        else:
            not_found.append(cid)
    return {"deleted": deleted, "not_found": not_found, "active_total": cookie_pool.size()}


@_api.post("/admin/cookies/{cookie_id}/enable")
def enable_cookie(cookie_id: int):
    """手动启用指定 Cookie，重置 fail_count 和冷却状态。"""
    records = cookie_pool.list_all()
    target = next((r for r in records if r.id == cookie_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail=f"Cookie id={cookie_id} 不存在")
    cookie_pool.update_cookie(target.value, is_active=True)
    # 重置 fail_count 和 cooldown：借助 mark_success 机制
    cookie_pool.mark_success(target.value)
    return {"success": True, "active_total": cookie_pool.size()}


@_api.post("/admin/cookies/{cookie_id}/disable")
def disable_cookie(cookie_id: int):
    """手动停用指定 Cookie。"""
    records = cookie_pool.list_all()
    target = next((r for r in records if r.id == cookie_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail=f"Cookie id={cookie_id} 不存在")
    ok = cookie_pool.update_cookie(target.value, is_active=False)
    if not ok:
        raise HTTPException(status_code=500, detail="停用失败")
    return {"success": True, "active_total": cookie_pool.size()}


@_api.post("/admin/cookies/{cookie_id}/check")
async def check_cookie(cookie_id: int):
    """立即对指定 Cookie 触发健康检查，返回检查结果。"""
    from app.cookie_health import trigger_check_one
    records = cookie_pool.list_all()
    target = next((r for r in records if r.id == cookie_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail=f"Cookie id={cookie_id} 不存在")
    result = await trigger_check_one(target.value)
    return {"cookie_id": cookie_id, **result, "active_total": cookie_pool.size()}


@_api.post("/admin/cookies/check-all")
async def check_all_cookies():
    """立即对所有 Cookie 触发全量健康检查，返回汇总结果。"""
    from app.cookie_health import trigger_check_all
    result = await trigger_check_all()
    return {**result, "active_total": cookie_pool.size()}


@_api.get("/admin/cookies/export")
def export_cookies():
    """导出所有 Cookie 完整记录为 JSON 文件下载（含原始值）。"""
    records = cookie_pool.list_all()
    data = [_record_to_dict(r, include_raw=True) for r in records]
    return JSONResponse(
        content={"cookies": data, "total": len(data)},
        headers={"Content-Disposition": "attachment; filename=cookies_export.json"},
    )


# 将受保护的 API 路由挂载到主 router
router.include_router(_api)
