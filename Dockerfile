# syntax=docker/dockerfile:1.7
#
#   docker run --rm <image> oex-cli --help
#   docker run --rm -v $PWD/output:/app/output <image> just run osm nepal buildings

FROM ghcr.io/astral-sh/uv:0.11.8-python3.13-trixie-slim AS builder

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
    uv sync --frozen --no-dev --no-editable


FROM debian:trixie-slim AS tippecanoe-build

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential curl ca-certificates libsqlite3-dev zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*
COPY scripts/install-tippecanoe.sh /tmp/install-tippecanoe.sh
RUN /tmp/install-tippecanoe.sh


FROM ghcr.io/astral-sh/uv:0.11.8-python3.13-trixie-slim AS runtime

RUN apt-get update \
    && apt-get install -y --no-install-recommends osmium-tool just \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 1000 app

WORKDIR /app

COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --from=tippecanoe-build /usr/local/bin/tippecanoe /usr/local/bin/tippecanoe
COPY --chown=app:app justfile /app/justfile
COPY --chown=app:app configs /app/configs
COPY --chown=app:app hot_countries.txt /app/hot_countries.txt

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    JUST_JUSTFILE=/app/justfile

RUN mkdir -p /app/output /app/data /tmp/duckdb_temp && chown app:app /app/output /app/data /tmp/duckdb_temp
USER app

# No ENTRYPOINT: the caller picks `oex-cli`, `just`, or any other command.
CMD ["oex-cli", "--help"]
