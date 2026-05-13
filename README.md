# oex

**Open Data Exporter.** Country-scale vector data from OpenStreetMap and
Overture Maps, exported to GeoPackage, Shapefile, GeoJSON, or KML. Optional
HDX publication.

## Install

One-line installer (picks `uv`, `pipx`, or `pip --user`, whichever you have):

```bash
curl -LsSf https://raw.githubusercontent.com/osgeonepal/oex/main/scripts/install.sh | sh
```

Or pick directly:

```bash
uv tool install oex          # uv
pipx install oex             # pipx
pip install --user oex       # pip
```

Or run the docker image without installing anything:

```bash
docker run --rm -v "$PWD/output:/app/output" \
  ghcr.io/osgeonepal/oex:latest oex-cli osm npl
```

For docker as a system command:

```bash
curl -LsSf https://raw.githubusercontent.com/osgeonepal/oex/main/scripts/install.sh | sh -s -- --docker
```

That writes `/usr/local/bin/oex-cli` wrapping the docker image so
`oex-cli osm npl` runs the container.

## One line, country in your hands

```bash
oex-cli osm npl
```

Eight categories (Buildings, Roads, Hospitals, Schools, Rivers, Land Use,
Transportation Hubs, Settlements) for Nepal as gpkg + shp zips in
`./output/`. Replace `npl` with any ISO3. Use `oex-cli overture <iso3>`
for Overture Maps instead.

## Two ways to customise

```bash
# Curated schema (12-layer HOT-style HDX export, Overture data package, etc)
oex-cli osm --config configs/examples/hot-schema.yaml --iso3 NPL

# Your own categories
oex-cli osm --config ./my-stuff.yaml
```

See **[Get started](https://osgeonepal.github.io/oex/get-started/)** for
the install matrix and three flows in detail, **[Custom categories](https://osgeonepal.github.io/oex/custom-categories/)**
for the schema, and **[HDX publication](https://osgeonepal.github.io/oex/hdx/)**
for pushing to HDX.

## Features

- 8 default categories: Buildings, Roads, Hospitals, Schools, Rivers,
  Land Use, Transportation Hubs, Settlements.
- 12-layer HOT-style HDX schema mirroring `hotosm_<iso3>_*` exports.
- 15-dataset Overture data package (one per theme/feature type).
- Output formats: `gpkg`, `shp`, `geojson`, `kml`. Default is `[gpkg, shp]`.
- Administrative pcode tagging: each feature gets `adm0`-`adm4` pcode and
  name columns from fieldmaps.io humanitarian boundaries.
- Name transliteration to Latin script (`name_latin`) via unidecode, with
  `name_en` preferred when present.
- ISO3 language columns (`name_hi`, `name_ar`, `name_ne`, ...) resolved via
  pycountry + babel.
- Per-category export report: feature count, bbox, geometry types, temporal
  range, null %, distinct counts, and top values per column.
- Brazil HOT 12-category run: ~22 M features in ~63 min, peak ~5.7 GB RAM.

## Develop from source

```bash
git clone https://github.com/osgeonepal/oex
cd oex
just setup
just test
just osm nepal
```

## Stack

| Concern              | Tool                                                                              |
| -------------------- | --------------------------------------------------------------------------------- |
| Package manager      | [uv](https://github.com/astral-sh/uv)                                             |
| Build backend        | [uv_build](https://docs.astral.sh/uv/concepts/build-backend/)                     |
| Linter + formatter   | [ruff](https://github.com/astral-sh/ruff)                                         |
| Type checker         | [ty](https://github.com/astral-sh/ty)                                             |
| Tests                | [pytest](https://docs.pytest.org/) + pytest-cov                                   |
| Task runner          | [just](https://github.com/casey/just)                                             |
| Query engine         | [DuckDB](https://duckdb.org/) + spatial extension                                 |
| OSM parser           | [QuackOSM](https://github.com/kraina-ai/quackosm)                                 |
| Overture access      | DuckDB httpfs over [s3://overturemaps-us-west-2](https://docs.overturemaps.org/)  |
| Boundaries (default) | [geoBoundaries CGAZ ADM0](https://www.geoboundaries.org/)                         |
| Pcode boundaries     | [fieldmaps.io](https://fieldmaps.io/) humanitarian admin boundaries               |
| Pcode index          | [H3](https://h3geo.org/) hexagonal cell index at resolution 7                     |

## License

GPL-3.0-only. See [LICENSE](LICENSE).
