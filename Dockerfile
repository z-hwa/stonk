# Cloud Run image: positions Web UI 專用 (不含 pandas/yfinance,image ~150MB)
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements-server.txt .
RUN pip install -r requirements-server.txt

COPY positions_store.py positions_server.py ./

# Cloud Run 會注入 PORT env (預設 8080)
ENV PORT=8080
EXPOSE 8080

CMD ["sh", "-c", "uvicorn positions_server:app --host 0.0.0.0 --port ${PORT}"]
