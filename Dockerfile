# Stage 1: Frontend SPA Build
FROM node:20-alpine AS frontend-builder
WORKDIR /build
COPY frontend/package*.json ./
RUN npm ci || npm install
COPY frontend/ ./
RUN npm run build

# Stage 2: Python 3.12 Runtime
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=5250
WORKDIR /app
RUN apt-get update && \
    apt-get install -y --no-install-recommends gosu libchromaprint-tools && \
    rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY plex_playlist_sync ./plex_playlist_sync
COPY pyproject.toml README.md ./
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
COPY --from=frontend-builder /build/dist ./frontend/dist
RUN useradd --create-home --uid 1000 appuser && \
    mkdir -p /config /data /data/media/music /data/downloads /music /downloads && \
    chown -R appuser:appuser /app /config /data /music /downloads
EXPOSE 5250
VOLUME ["/config", "/data"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5250/api/health')" || exit 1
ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "-m", "plex_playlist_sync"]