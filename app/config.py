import os
from dotenv import load_dotenv

load_dotenv()

BASE_URL = "https://workspace.nexos.ai"

SERVER_HOST = os.getenv("HOST", "0.0.0.0")
SERVER_PORT = int(os.getenv("PORT", "3000"))

# 动态获取失败时的 fallback handler ID
DEFAULT_HANDLER_ID = os.getenv("NEXOS_DEFAULT_HANDLER_ID", "")
DEFAULT_CHAT_ID = os.getenv("NEXOS_CHAT_ID", "")

# Cookie 存储后端配置
# COOKIE_STORE_BACKEND: sqlite（默认）或 redis
COOKIE_STORE_BACKEND: str = os.getenv("COOKIE_STORE_BACKEND", "sqlite").lower()
COOKIE_DB_PATH: str = os.getenv("COOKIE_DB_PATH", "cookies.db")
REDIS_URL: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")
# Cookie 连续失败超过此阈值后自动标记为 inactive
COOKIE_MAX_FAIL_COUNT: int = int(os.getenv("COOKIE_MAX_FAIL_COUNT", "5"))
# Cookie 失败后冷却时长（秒），冷却期内不参与轮询；0 表示不冷却
COOKIE_COOLDOWN_SECONDS: int = int(os.getenv("COOKIE_COOLDOWN_SECONDS", "60"))

# 禁用对话历史：为 true 时每次请求创建全新 chat_id，不复用也不持久化绑定
DISABLE_HISTORY: bool = os.getenv("DISABLE_HISTORY", "false").lower() == "true"

# Cookie 健康检查配置
COOKIE_HEALTH_CHECK_INTERVAL: int = int(os.getenv("COOKIE_HEALTH_CHECK_INTERVAL", "300"))
COOKIE_AUTO_RECOVER: bool = os.getenv("COOKIE_AUTO_RECOVER", "false").lower() == "true"

# Cookie 轮询策略：round-robin | weighted | least-used
COOKIE_ROTATION_STRATEGY: str = os.getenv("COOKIE_ROTATION_STRATEGY", "weighted").lower()

# 请求日志配置
ENABLE_REQUEST_LOGGING: bool = os.getenv("ENABLE_REQUEST_LOGGING", "true").lower() == "true"
LOG_RETENTION_DAYS: int = int(os.getenv("LOG_RETENTION_DAYS", "7"))

# 管理界面访问控制（留空则不验证）
ADMIN_PASSWORD: str = os.getenv("ADMIN_PASSWORD", "")

# 出口代理（可选）：支持 http://、https://、socks5:// 等格式
# 留空表示不使用代理，SOCKS 协议需额外安装 httpx[socks]
PROXY_URL: str = os.getenv("PROXY_URL", "")

# SSL 验证：设为 false 可禁用 TLS 证书校验（仅限受信内网，生产环境请保持 true）
NEXOS_VERIFY_SSL: bool = os.getenv("NEXOS_VERIFY_SSL", "true").lower() != "false"

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
