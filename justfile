set shell := ["bash", "-uc"]

# Default: list available recipes
default:
    @just --list

# Sync the dev environment (creates .venv) and install pre-commit hooks
setup:
    uv sync --all-groups
    uv run pre-commit install --install-hooks --hook-type pre-commit --hook-type commit-msg

# Run all pre-commit hooks (ruff fix, ruff format, ty, file hygiene)
lint:
    uv run pre-commit run --all-files

# Run the test suite with coverage (skips integration tests by default)
test *ARGS:
    uv run pytest -m "not integration" {{ARGS}}

# Run integration tests (network + larger downloads)
test-integration *ARGS:
    uv run pytest -m integration {{ARGS}}

# Build sdist and wheel into dist/
build:
    uv build

# Run the Overture exporter.
# Examples:
#   just overture nepal
#   just overture nepal buildings
#   just overture --configs-dir configs/
overture *ARGS:
    uv run oex-cli overture {{ARGS}}

# Run the OSM exporter.
# Examples:
#   just osm nepal
#   just osm nepal buildings
#   just osm --configs-dir configs/
osm *ARGS:
    uv run oex-cli osm {{ARGS}}

# Build the OSM PBF -> Hilbert-sorted GeoParquet cache.
# Examples:
#   just osm-build-cache --planet
#   just osm-build-cache --pbf /path/to/nepal.osm.pbf
osm-build-cache *ARGS:
    uv run oex-cli osm-build-cache {{ARGS}}
