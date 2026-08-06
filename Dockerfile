# Multi-stage: build the React frontend, then the uv-managed Python app.
# One image serves both FastAPI apps; compose picks the command per service.

FROM node:22-alpine AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

# Dependency layer first so code edits don't re-resolve the environment.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

COPY README.md ./
COPY src/ src/
RUN uv sync --frozen --no-dev

COPY --from=frontend /build/dist /app/frontend/dist

# Databases live on a mounted volume; paths are container-fixed so the compose
# file stays free of path plumbing. The frontend dist path must be explicit —
# api.py's source-tree-relative fallback doesn't apply to an installed package.
ENV PATH="/app/.venv/bin:$PATH" \
    MLB_ODDS_DB=/data/odds.sqlite \
    NFL_ODDS_DB=/data/nfl-odds.sqlite \
    CONTEST_DB=/data/contest.sqlite \
    MLB_ODDS_FRONTEND_DIST=/app/frontend/dist

VOLUME /data
EXPOSE 8000 8001
CMD ["uvicorn", "mlb_odds.api:app", "--host", "0.0.0.0", "--port", "8000"]
