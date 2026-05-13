# oex

**Open Data Exporter.** Country-scale vector data from OpenStreetMap and
Overture Maps, exported to GeoPackage, Shapefile, GeoJSON, or KML. Optional
HDX publication.

## Install

```bash
curl -LsSf https://raw.githubusercontent.com/osgeonepal/oex/main/scripts/install.sh | sh
```

That installs the `oex-cli` command using whichever of `uv`, `pipx`, or
`pip --user` is on your machine. Other paths:

```bash
uv tool install oex
pipx install oex
pip install --user oex
```

Or run the docker image directly:

```bash
docker run --rm -v "$PWD/output:/app/output" \
  ghcr.io/osgeonepal/oex:latest oex-cli osm npl
```

For docker as a system command:

```bash
curl -LsSf https://raw.githubusercontent.com/osgeonepal/oex/main/scripts/install.sh | sh -s -- --docker
```

That writes `/usr/local/bin/oex-cli` wrapping the docker image.

## Three flows

```bash
# 1. Download a country with the bundled defaults
oex-cli osm npl

# 2. Use a curated category set (HOT-style, Overture data package, ...)
oex-cli osm --config configs/examples/hot-schema.yaml --iso3 NPL

# 3. Define your own categories
oex-cli osm --config ./my-stuff.yaml
```

Replace `npl` with any ISO3. Switch `osm` for `overture` to pull from
Overture Maps instead.

Outputs land in `./output/<iso3>/<source>/<key>_<iso3>_<category>_<format>.zip`.
Each zip contains the GIS file(s), `README.txt`, `config.yaml`, and
optionally `metadata.json` (when `output.metadata: true`) with per-column
null shares, distinct counts, geometry types, and bbox.

## Features

- 8 default categories: Buildings, Roads, Hospitals, Schools, Rivers,
  Land Use, Transportation Hubs, Settlements.
- 12-layer HOT-style HDX schema mirroring `hotosm_<iso3>_*` exports.
- 15-dataset Overture data package (one per `theme`/`feature_type`).
- Output formats: `gpkg`, `shp`, `geojson`, `kml`. Default is `[gpkg, shp]`.
- Two OSM engines: per-country PBF on demand (Geofabrik) or planet PBF
  cache (for monthly batch runs across many countries).
- **Administrative pcode tagging**: each feature gets `adm0`-`adm4` pcode
  and name columns from fieldmaps.io humanitarian boundaries (opt-in,
  enabled by default in the HOT schema).

## Stack

| Concern              | Tool                                                                              |
| -------------------- | --------------------------------------------------------------------------------- |
| Query engine         | [DuckDB](https://duckdb.org/) + spatial extension                                 |
| OSM parser           | [QuackOSM](https://github.com/kraina-ai/quackosm)                                 |
| Overture access      | DuckDB httpfs over [s3://overturemaps-us-west-2](https://docs.overturemaps.org/)  |
| Boundaries (default) | [geoBoundaries CGAZ ADM0](https://www.geoboundaries.org/)                         |
| Package manager      | [uv](https://github.com/astral-sh/uv)                                             |
| Linter / type-check  | [ruff](https://github.com/astral-sh/ruff) / [ty](https://github.com/astral-sh/ty) |

## Where next

- **[Architecture](architecture.md)**: data flow diagram, technical pipeline, and Brazil benchmark
- **[Get started](get-started.md)**: install matrix and three flows in detail
- **[Custom categories](custom-categories.md)**: write your own SQL select / OSM tag filter
- **[Configuration](config.md)**: full schema reference
- **[HDX publication](hdx.md)**: pushing to data.humdata.org
- **[CLI and library](usage.md)**: Python API + CLI reference
