# ── 构建阶段 ────────────────────────────────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /build

# 仅复制依赖声明，充分利用层缓存
COPY requirements.txt .

RUN pip install --upgrade pip --no-cache-dir \
 && pip install --no-cache-dir --prefix=/install -r requirements.txt

# ── 运行阶段 ────────────────────────────────────────────────────────────────
FROM python:3.11-slim AS runtime

# 安全：以非 root 用户运行
RUN groupadd -r appuser && useradd -r -g appuser appuser

WORKDIR /app

# 从构建阶段复制已安装的依赖
COPY --from=builder /install /usr/local

# 复制应用代码
COPY app/ ./app/
COPY templates/ ./templates/
COPY main.py .

# 数据目录（SQLite 持久化挂载点）
RUN mkdir -p /data && chown appuser:appuser /data

USER appuser

# 默认环境变量
ENV HOST=0.0.0.0 \
    PORT=3000 \
    COOKIE_DB_PATH=/data/cookies.db

EXPOSE 3000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:3000/')"

CMD ["python", "-m", "uvicorn", "main:app", \
     "--host", "0.0.0.0", \
     "--port", "3000", \
     "--workers", "1", \
     "--no-access-log"]
