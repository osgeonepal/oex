# Changelog

All notable changes to this project will be documented in this file.

This project follows [Conventional Commits](https://www.conventionalcommits.org/)
and is auto-managed by [commitizen](https://commitizen-tools.github.io/commitizen/).

## v0.2.0 (unreleased)

### Breaking

- Renamed package from `overture2hdx` to `oex` (Open Data Exporter).
  Distribution name `oex`, import `oex`, CLI command `oex-cli`.
- Repository moved to <https://github.com/osgeonepal/oex>.
- Reworked public API around two sources (Overture, OSM) and a shared exporter core.
- Schema cleanup: removed `osm.theme` (cache slug auto-derived from category name);
  renamed `osm.osm_tags` to `osm.filter` with explicit "build-time" docstring.
- Switched build/tooling to `uv_build`, `ruff`, `ty`, `pytest`, `commitizen`, `just`.
- Bumped minimum Python to 3.11.

### Feat

- New OSM source with two engines:
  - `geofabrik` (default): on-demand per-country PBF download from Geofabrik's
    `index-v1.json`, ISO3 to ISO2 mapping via `pycountry`, quackosm-driven
    conversion to per-country GeoParquet cache, optional clip to the country
    boundary so output edges are exact.
  - `planet_parquet`: Hilbert-sorted per-theme planet cache built once via
    `oex-cli osm-build-cache --planet`, then queried for any country.
- New Overture source: pinned to `overturemaps==1.0.0` with DuckDB-direct engine
  against the Overture S3 release bucket; latest release auto-resolved from
  the public S3 listing.
- Typed config via OmegaConf with dataclass schemas, per-country YAML composition.
- `categories_file` top-level field to load a reusable custom schema set
  alongside or in place of the bundled defaults.
- Default ADM0 boundary fallback via geoBoundaries CGAZ.
- Optional HDX publication gated by `hdx.push`.
- Typer-based CLI: `oex-cli overture|osm <iso3> [theme]`,
  `--engine geofabrik|planet_parquet`, `--configs-dir` fan-out for batch runs.
- Tuned DuckDB httpfs retry/timeout settings so transient S3 blips don't kill
  long-running multi-category jobs.
- Optional per-dataset metadata report (`output.metadata: true`): embeds a
  `metadata.json` in each zip with feature count, geometry-type breakdown, bbox,
  and per-column null share, distinct count, and top-5 values.
- One-line installer at `scripts/install.sh` (curl-pipe-bash). Picks the best
  available installer (uv, pipx, pip --user) or installs a docker wrapper at
  `/usr/local/bin/oex-cli` with `--docker`.
- Drop-in docker image (`ghcr.io/osgeonepal/oex:latest`) bundles `just` + the
  project `justfile` + `configs/examples/`. ~234 MB content size.
