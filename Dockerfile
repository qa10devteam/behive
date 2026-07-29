# ══════════════════════════════════════════════════════════════
# 🐝 BeHive — Deep Research Engine
# Multi-stage build for minimal production image
# ══════════════════════════════════════════════════════════════

FROM python:3.11-slim AS builder

WORKDIR /app

# System deps for building
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ libpq-dev curl && \
    rm -rf /var/lib/apt/lists/*

# Install Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ──────────────────────────────────────────────────────────────
FROM python:3.11-slim AS runtime

WORKDIR /app

# Runtime deps only
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 curl \
    # Chromium deps for Playwright (browser-based search)
    libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
    libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
    libgbm1 libpango-1.0-0 libcairo2 libasound2 libatspi2.0-0 && \
    rm -rf /var/lib/apt/lists/* && \
    useradd -m -r behive

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy application
COPY . .

# Install the behive package + Playwright browsers
RUN pip install --no-cache-dir -e . && \
    pip install --no-cache-dir playwright && \
    playwright install chromium --with-deps 2>/dev/null || true

# Create data directories
RUN mkdir -p /data/reports /data/cache && chown -R behive:behive /app /data

USER behive

# Health check
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:8091/health || exit 1

EXPOSE 8091 8090

ENV BEHIVE_DATA_DIR=/data \
    BEHIVE_HOST=0.0.0.0 \
    BEHIVE_PORT=8091 \
    BEHIVE_MCP_PORT=8090 \
    PYTHONUNBUFFERED=1

CMD ["behive", "serve", "--host", "0.0.0.0", "--port", "8091", "--mcp-port", "8090"]
