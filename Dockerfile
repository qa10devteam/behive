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
    libpq5 curl && \
    rm -rf /var/lib/apt/lists/* && \
    useradd -m -r behive

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy application
COPY . .

# Create data directories
RUN mkdir -p /data/reports /data/cache && chown -R behive:behive /app /data

USER behive

# Health check
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:8091/health || exit 1

EXPOSE 8091

ENV BEHIVE_DATA_DIR=/data \
    BEHIVE_HOST=0.0.0.0 \
    BEHIVE_PORT=8091 \
    PYTHONUNBUFFERED=1

CMD ["python", "-m", "uvicorn", "behive_api:app", "--host", "0.0.0.0", "--port", "8091"]
