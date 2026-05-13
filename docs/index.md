# oex

**Open Data Exporter.** Country-scale vector data from OpenStreetMap and
Overture Maps, exported to GeoPackage, Shapefile, GeoJSON, or KML. Optional
HDX publication.

## How it works

```mermaid
flowchart LR
    CLI["oex-cli osm BRA\n--config hot.yaml"]

    subgraph boundary["Boundary"]
        GBND["geoBoundaries CGAZ ADM0\n(default)"]
        CBND["Custom GeoJSON\n(boundary.geom)"]
    end

    subgraph acquire["Data acquisition"]
        PBF["Geofabrik PBF\n50–500 MB per country"]
        PLANET["Planet PBF\nosmium clip + buffer\n→ country PBF"]
        QOSM["QuackOSM\nPBF → GeoParquet\ntag-filtered cache"]
        OVT["Overture Maps\nS3 parquet\nDuckDB httpfs"]
        PBF -->|"country not published"| PLANET
        PBF --> QOSM
        PLANET --> QOSM
    end

    subgraph process["Per-category loop  ·  DuckDB in-process"]
        SQL["SQL SELECT\nbbox clip + ST_Within boundary"]
        H3["Pcode tagging\nfieldmaps.io boundaries\nH3 cell index join\n+ ST_Contains fallback"]
        TRANSLIT["Transliteration\nname → name_latin\nunidecode + name_en prefer"]
        ISO["ISO3 language columns\nname_hi · name_ar · name_ne …\npycountry + babel"]
        FMT["Format writers\ngpkg · shp · geojson · kml"]
        SQL --> H3 --> TRANSLIT --> ISO --> META
        META["Export report\nfeature count · bbox\ngeometry types · temporal range\nnull % · distinct counts · top values"]
        META --> FMT
    end

    CLI --> boundary
    CLI --> acquire
    boundary --> process
    QOSM --> process
    OVT --> process
    FMT --> ZIP["zip bundle\nREADME + metadata.json\n+ report.html"]
    ZIP --> OUT["HDX · S3"]
```

1. **Boundary** - resolves the country polygon from geoBoundaries CGAZ ADM0
   (or a custom GeoJSON) and derives a bounding box for all spatial queries.
2. **Data** - OSM: downloads the per-country PBF from Geofabrik (or clips
   from a local planet PBF) and converts it to GeoParquet once via QuackOSM.
   Overture: reads parquet directly from S3 over DuckDB httpfs, no download.
3. **Export loop** - for each category, DuckDB clips the cached parquet to
   the country boundary, applies column and tag filters, optionally tags every
   feature with administrative pcodes (adm1-adm4), transliterates names to
   Latin script, and writes the requested output formats.
4. **Bundle** - each format is zipped with a README and optional metadata
   JSON, then optionally uploaded to HDX or S3.

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

## Technical pipeline

**Query engine.** DuckDB runs embedded in the Python process and reads
GeoParquet files via memory-mapped columnar scans. Per-category exports are
DuckDB SQL SELECT statements with a bbox clip and `ST_Within` boundary filter.

**OSM path: Geofabrik → planet fallback → GeoParquet cache.**

The default OSM engine downloads the per-country PBF from Geofabrik.
For countries Geofabrik does not publish, oex falls back to a local planet
PBF:

1. The country boundary polygon is expanded by the configured
   `buffer_meters`, reprojected to EPSG:3857, buffered, then reprojected
   back to WGS84.
2. `osmium extract --strategy=complete_ways` clips the planet PBF to that
   polygon, producing a country-sized PBF.
3. [QuackOSM](https://github.com/kraina-ai/quackosm) converts the PBF to
   GeoParquet with tag filtering applied at parse time. The result is written
   to a local cache and reused on subsequent runs.
4. Per-category queries run as DuckDB SELECT statements over the cached
   parquet.

**Overture path: S3 parquet via httpfs.**
Overture Maps publishes release parquet at
`s3://overturemaps-us-west-2/release/<release>/theme=.../type=.../`.
DuckDB's httpfs extension reads these files with parallel HTTP range
requests, applying the bbox filter at the parquet page level.

**Pcode tagging: H3 index join.**
Admin pcodes are assigned in three stages:

1. **Cover** - each admin polygon (adm1-adm4) is filled with
   [H3](https://h3geo.org/) hexagonal cells at resolution 7 (~5.16 km²
   per cell). `MULTIPOLYGON` geometries are first decomposed into their
   constituent parts before coverage.
2. **Index** - each feature's centroid is converted to the H3 cell ID at
   the same resolution. The centroid is used for attribution: a building,
   road segment, school, or POI is assigned to the admin area it sits inside.
3. **Join** - the centroid cell ID is joined against the admin cell lookup
   on integer equality. Features whose centroid falls on a shared cell
   boundary are resolved with a `ST_Contains` point-in-polygon check.

## Performance

Full HOT 12-category schema for **Brazil** on a 20 GB Docker container,
single worker, 4 CPU cores. DuckDB memory limit: 60% of container RAM (~12 GB).

| Category            | Features   | Export time |
| ------------------- | ---------- | ----------- |
| Buildings           | 11,246,007 | 7.5 min     |
| Roads               | 8,000,187  | 12.7 min    |
| Waterways           | 1,870,423  | 13.4 min    |
| Railways            | 17,269     | 3.2 min     |
| Education           | 111,358    | 3.5 min     |
| Health              | 43,989     | 3.1 min     |
| Populated places    | 197,969    | 3.5 min     |
| Financial services  | 17,409     | 2.7 min     |
| Airports            | 28,392     | 2.8 min     |
| Sea ports           | 1,723      | 2.6 min     |
| Points of interest  | 846,550    | 4.7 min     |
| Cultural places     | 88,388     | 2.9 min     |
| **Total**           | **~22 M**  | **~63 min** |

**Peak memory: ~5.7 GB** across the full run (measured during pcode tagging
of the 11.2 M building category).

Pcode tagging cost is largely independent of feature count: Brazil's admin
tessellation produces ~1.54 M H3 cells per admin level and all four levels
are built per category, accounting for ~3-4 min of each category's runtime.

## Where next

- **[Get started](get-started.md)**: install matrix and three flows in detail
- **[Custom categories](custom-categories.md)**: write your own SQL select / OSM tag filter
- **[Configuration](config.md)**: full schema reference
- **[HDX publication](hdx.md)**: pushing to data.humdata.org
- **[CLI and library](usage.md)**: Python API + CLI reference
