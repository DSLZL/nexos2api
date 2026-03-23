"""FastAPI 应用入口。"""
import asyncio
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from app.config import SERVER_HOST, SERVER_PORT
from app.cookie_pool import list_all
from app.cookie_health import health_check_loop
from app.model_registry import get_model_mapping
from app.routes import chat, chat_mgmt, cookies, files, models
from app.routes.stats import router as stats_router


async def _warmup() -> None:
    """启动时并发预热所有活跃 cookie 的模型缓存。"""
    records = [r for r in list_all() if r.is_active]
    if not records:
        print("[warmup] 无活跃 Cookie，跳过预热")
        return
    print(f"[warmup] 开始并发预热 {len(records)} 个 Cookie 的模型缓存...")
    results = await asyncio.gather(
        *[get_model_mapping(r.value) for r in records],
        return_exceptions=True,
    )
    ok = sum(1 for r in results if not isinstance(r, Exception))
    fail = len(results) - ok
    print(f"[warmup] 完成：{ok} 成功，{fail} 失败")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await _warmup()
    health_task = asyncio.create_task(health_check_loop())
    try:
        yield
    finally:
        health_task.cancel()
        try:
            await health_task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="Nexos2API", version="1.0.0", lifespan=lifespan)

app.include_router(models.router)
app.include_router(files.router)
app.include_router(chat.router)
app.include_router(chat_mgmt.router)
app.include_router(cookies.router)
app.include_router(stats_router)


@app.get("/")
async def root():
    return {"status": "ok", "message": "Nexos2API is running"}


if __name__ == "__main__":
    print(f"Starting Nexos2API on {SERVER_HOST}:{SERVER_PORT}")
    uvicorn.run("main:app", host=SERVER_HOST, port=SERVER_PORT, reload=False)
