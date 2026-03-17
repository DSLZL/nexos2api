import os
from dotenv import load_dotenv

load_dotenv()

BASE_URL = "https://workspace.nexos.ai"

SERVER_HOST = os.getenv("HOST", "0.0.0.0")
SERVER_PORT = int(os.getenv("PORT", "3000"))

# 动态获取失败时的 fallback handler ID
DEFAULT_HANDLER_ID = os.getenv("NEXOS_DEFAULT_HANDLER_ID", "")
DEFAULT_CHAT_ID = os.getenv("NEXOS_CHAT_ID", "")
DISABLE_HISTORY = os.getenv("DISABLE_HISTORY", "false").lower() == "true"

COMMON_HEADERS: dict = {
    "accept": "*/*",
    "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
    "cache-control": "no-cache",
    "x-timezone": "Asia/Shanghai",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/146.0.0.0 Safari/537.36"
    ),
}
