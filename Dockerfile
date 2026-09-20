# Multi-platform Dockerfile for buku digital book server (linux/amd64, linux/arm64)
FROM ghcr.io/astral-sh/uv:latest AS uv_bin

FROM python:3.14-slim

# Copy uv binaries
COPY --from=uv_bin /uv /uvx /bin/

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

# Copy dependency specifications first for Docker layer caching
COPY pyproject.toml uv.lock ./

# Install project dependencies
RUN uv sync --frozen --no-dev --no-install-project

# Copy project source
COPY README.md ./
COPY src/ ./src/

# Finalize project install
RUN uv sync --frozen --no-dev

# Ensure virtualenv binaries are on PATH
ENV PATH="/app/.venv/bin:$PATH"

# Create non-root user and establish standard directory conventions
RUN useradd -m -u 1000 -s /bin/bash bookuser && \
    mkdir -p /config /books && \
    chown -R bookuser:bookuser /config && \
    chown -R bookuser:bookuser /app

USER bookuser

VOLUME ["/config", "/books"]

ENV BUKU_CONFIG_DIR=/config \
    BUKU_BOOKS_DIR=/books \
    BUKU_HOST=0.0.0.0 \
    BUKU_PORT=8080

EXPOSE 8080

ENTRYPOINT ["bookserver", "serve"]
