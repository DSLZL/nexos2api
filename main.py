"""FastAPI 应用入口。"""
import uvicorn
from fastapi import FastAPI

from app.config import SERVER_HOST, SERVER_PORT
from app.routes import chat, chat_mgmt, files, models

app = FastAPI(title="Nexos2API", version="1.0.0")

app.include_router(models.router)
app.include_router(files.router)
app.include_router(chat.router)
app.include_router(chat_mgmt.router)


@app.get("/")
async def root():
    return {"status": "ok", "message": "Nexos2API is running"}


if __name__ == "__main__":
    print(f"Starting Nexos2API on {SERVER_HOST}:{SERVER_PORT}")
    uvicorn.run("main:app", host=SERVER_HOST, port=SERVER_PORT, reload=False)
