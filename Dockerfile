# syntax=docker/dockerfile:1.7
#
# Multi-stage build:
# The container is a drop-in standalone CLI:
#   docker run --rm <image> oex-cli --help
#   docker run --rm <image> just overture nepal buildings
#   docker run --rm -v $PWD/output:/app/output <image> just overture nepal

FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

COPY README.md LICENSE ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev


FROM debian:bookworm-slim AS just-fetch

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && curl --proto '=https' --tlsv1.2 -sSfL https://just.systems/install.sh \
    | bash -s -- --to /usr/local/bin


FROM python:3.13-slim-bookworm AS runtime

RUN useradd --create-home --uid 1000 app

WORKDIR /app

COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --from=builder --chown=app:app /app/src /app/src
COPY --from=just-fetch /usr/local/bin/just /usr/local/bin/just
COPY --chown=app:app justfile /app/justfile
COPY --chown=app:app configs /app/configs

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    JUST_JUSTFILE=/app/justfile

RUN mkdir -p /app/output /app/data && chown app:app /app/output /app/data
USER app

# No ENTRYPOINT: caller picks `oex-cli`, `just`, or any other command.
CMD ["oex-cli", "--help"]
