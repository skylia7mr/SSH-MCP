FROM node:22-bookworm-slim AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim-bookworm AS backend
COPY --from=ghcr.io/astral-sh/uv:0.11.0 /uv /uvx /usr/local/bin/
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src/ ./src/
RUN uv sync --frozen --no-dev

FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends rsync && rm -rf /var/lib/apt/lists/*
COPY --from=backend /usr/local/bin/uv /usr/local/bin/uv
WORKDIR /app
COPY --from=backend /app /app
COPY --from=frontend /build/dist /app/frontend/dist
RUN groupadd --gid 10001 workbench && useradd --uid 10001 --gid 10001 --no-create-home workbench && mkdir /app/trust && chown 10001:10001 /app/trust
ENV SSH_MCP_HOST=0.0.0.0 SSH_MCP_PORT=8000 SSH_MCP_KNOWN_HOSTS=/app/trust/known_hosts SSH_MCP_TRANSFER_ROOT=/tmp/ssh-mcp-files SSH_MCP_TRANSFER_CACHE_ROOT=/tmp/ssh-mcp-relay PYTHONUNBUFFERED=1 UV_CACHE_DIR=/tmp/uv-cache
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=15s --retries=3 CMD ["uv", "run", "--no-sync", "python", "-c", "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('SSH_MCP_PORT','8000')+'/healthz',timeout=2).read()"]
CMD ["uv", "run", "--no-sync", "python", "-m", "ssh_mcp"]
