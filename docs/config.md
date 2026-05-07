# Configuration

Configuration is layered:

1. Bundled defaults at `src/oex/defaults/base.yaml`.
2. A user YAML (passed via `--config` or auto-located at `configs/<iso3>.yaml`).
3. CLI overrides (`--iso3`, `--hdx-push`, `--output-dir`, etc).

Layers merge with [OmegaConf](https://omegaconf.readthedocs.io/), with one
deviation: when a user YAML provides a `categories` list, it **replaces** the
default list rather than element-wise merging.

## Mental model: `filter`, `where`, `select`

- **`select`** lists the columns to keep in the output. Same on both sources.
  Pure SQL expressions, e.g. `names.primary AS name` (Overture) or
  `tags['name'][1] AS name` (OSM).
- **`where`** is an optional SQL filter on the rows that come out of the
  source. Combined with the implicit country bbox and boundary clip.
- **`filter`** (OSM only) is the OSM tag filter passed to quackosm at
  **parquet build time**. It decides which OSM features ever land in the
  cache. This is the OSM equivalent of a row filter, but it runs during PBF
  to GeoParquet conversion, not at DuckDB query time. Most categories only
  need `filter`; `osm.where` stays empty.

For Overture there is no `filter`: the upstream (theme, feature_type) pair
already names a partitioned subset, so `where` is the only filter.

## Boundary

```yaml
boundary:
  geom: null                       # optional inline GeoJSON (string)
  geoboundaries_release: CGAZ
  geoboundaries_level: ADM0
  buffer_meters: 0                 # outward buffer in metres (0 = off)
```

If `geom` is set (a GeoJSON string), it overrides the geoBoundaries lookup;
otherwise the boundary comes from geoBoundaries CGAZ ADM0 for the ISO3.

`buffer_meters` is an outward buffer applied to whatever boundary you end up
with. The geometry is reprojected from EPSG:4326 to EPSG:3857, buffered by
the given metre value, then reprojected back. 0 disables it. Use this for
coastal countries or for cross-border features whose centroid sits a few
hundred metres outside the legal boundary (jetties, bridges, airfields).

A note on engines: the buffer affects the SQL clip applied at query time.
For `source.osm.engine: geofabrik`, OSM data is bounded by the per-country
PBF that Geofabrik publishes, so the buffer can only widen the clip *up to*
what Geofabrik already includes (Geofabrik PBFs do contain a small overlap
beyond the legal border, but it is not unlimited). Switch to
`planet_parquet` if you need a buffer larger than Geofabrik's own slice.
Overture is not affected; it reads from the global S3 bucket.

## Output formats

`output.formats` (or per-category `formats`) accepts any subset of:

| Format    | Notes                                                                                              |
| --------- | -------------------------------------------------------------------------------------------------- |
| `gpkg`    | GeoPackage. Single file, all geometry types together. Recommended default.                         |
| `shp`     | ESRI Shapefile. Split by geometry type. Field names truncated to 10 chars.                         |
| `geojson` | Single-file text. Easy to inspect, can be large.                                                   |
| `kml`     | Opens in Google Earth and most desktop GIS. Single XML file; prefer gpkg above ~1M features.       |

Default is `[gpkg, shp]`.

## Top-level keys

| Key            | Type                | Notes                                         |
| -------------- | ------------------- | --------------------------------------------- |
| `iso3`         | string              | Required. ISO3 country code.                  |
| `key`          | string              | Required. Slug for HDX dataset names.         |
| `dataset_name` | string \| null      | Pretty country/region name in HDX titles.     |
| `subnational`  | bool                | Sets HDX `subnational` flag.                  |
| `frequency`    | string              | HDX expected update frequency.                |
| `boundary`     | block               | See `BoundaryConfig`.                         |
| `output`       | block               | Output directory and format list.             |
| `parallel`     | block               | DuckDB threads + memory + thread pool toggle. |
| `duckdb`       | block               | http retry/timeout, temp dir, object cache.   |
| `logging`      | block               | level, format string.                         |
| `hdx`          | block               | HDX site, push toggle, credentials.           |
| `source`       | `overture` + `osm`  | Per-source settings (release, cache dir).     |
| `categories`   | list                | Per-theme `name`, `hdx`, `overture`, `osm`.   |

## Categories

Each category carries:

- `name`: human label (also used as the HDX dataset suffix and as the OSM
  cache parquet filename).
- `formats`: optional override for `output.formats`.
- `hdx`: title, notes, tags, license, license_url, caveats.
- `overture`: `enabled`, `theme`, `feature_type`, `select`, `where`.
- `osm`: `enabled`, `filter`, `select`, `where`.

Both `overture.select` and `osm.select` are pure SQL fragments. The geometry
column is appended automatically.

## OSM source schema (what your SELECT runs against)

The OSM cache produced by quackosm has three columns:

| Column       | Type                       | What it is                                    |
| ------------ | -------------------------- | --------------------------------------------- |
| `feature_id` | `VARCHAR`                  | OSM type + id, e.g. `node/12345`              |
| `tags`       | `MAP<VARCHAR, VARCHAR>`    | All retained OSM tags as key->value           |
| `geometry`   | geometry                   | POINT, LINESTRING, POLYGON, MULTIPOLYGON, ... |

DuckDB MAP access returns a list, so use `tags['name'][1]` to get the
scalar value of the `name` key. Refer to the
[OSM tag wiki](https://wiki.openstreetmap.org/wiki/Map_features) for what
keys exist; common ones include `building`, `highway`, `amenity`,
`waterway`, `landuse`, `place`, `aeroway`, `railway`, `name`, `name:en`,
`addr:*`, `source`.

`osm.filter` accepts the quackosm tag-filter shape:

```yaml
osm:
  filter:
    building: true                          # any value of `building`
    highway: ["primary", "secondary"]       # only these values
    amenity: ["hospital", "clinic"]
```

## Overture source schema (what your SELECT runs against)

Overture publishes parquet at `s3://overturemaps-us-west-2/release/<release>/theme=<theme>/type=<feature_type>/`.
Each (theme, feature_type) has a documented column set. The current release
exposes:

| Theme            | Feature type        | Notable columns                                                          |
| ---------------- | ------------------- | ------------------------------------------------------------------------ |
| `addresses`      | `address`           | `id`, `country`, `postcode`, `street`, `number`, `unit`                  |
| `base`           | `bathymetry`        | `id`, `depth`                                                            |
| `base`           | `infrastructure`    | `id`, `names`, `subtype`, `class`                                        |
| `base`           | `land`              | `id`, `names`, `subtype`, `class`                                        |
| `base`           | `land_cover`        | `id`, `subtype`, `cartography.{min,max}_zoom`                            |
| `base`           | `land_use`          | `id`, `names`, `subtype`, `class`, `surface`                             |
| `base`           | `water`             | `id`, `names`, `subtype`, `class`, `is_salt`, `wikidata`                 |
| `buildings`      | `building`          | `id`, `names`, `class`, `subtype`, `height`, `num_floors`, `roof_*`      |
| `buildings`      | `building_part`     | `id`, `height`, `num_floors`                                             |
| `divisions`      | `division`          | `id`, `names`, `subtype`, `country`, `region`, `population`, `wikidata`  |
| `divisions`      | `division_area`     | `id`, `names`, `subtype`, `country`, `region`                            |
| `divisions`      | `division_boundary` | `id`, `subtype`, `class`                                                 |
| `places`         | `place`             | `id`, `names`, `categories`, `addresses`, `phones`, `websites`, `confidence` |
| `transportation` | `connector`         | `id`                                                                     |
| `transportation` | `segment`           | `id`, `names`, `class`, `subclass`, `subtype`, `road_surface`            |

For the authoritative schema (including types and nested struct shapes),
see the [Overture Maps schema reference](https://docs.overturemaps.org/schema/).
Note that types are renamed across releases (e.g. `boundary` became
`division_boundary` in 2026-04-15.0), so pin `source.overture.release` if
your config relies on a specific schema.

## Custom schemas

Three ways to plug in your own category set:

1. Inline `categories:` in your country YAML (replaces defaults wholesale).
2. `categories_file: path/to/schema.yaml` on the country YAML.
3. Both: `categories_file` loads the base set, then an inline
   `categories:` block overrides for that country.

Each category needs `name`, plus any of:

- `formats` (list): override the global `output.formats` for this category.
- `hdx`: HDX metadata (title, notes, tags, license, license_url, caveats).
- `overture`: `theme`, `feature_type`, `select` (SQL), `where` (SQL).
- `osm`: `filter` (quackosm tag filter), `select` (SQL), `where` (SQL).

## OSM source: engines

```yaml
source:
  osm:
    engine: geofabrik          # or planet_parquet
    cache_dir: data/osm
    snapshot: latest
    geofabrik_clip_to_boundary: true
```

`geofabrik` (default): no pre-build. First run per country downloads the PBF
and builds the cache. Subsequent runs reuse it. The cache lives at
`<cache_dir>/geofabrik/<iso3>/<snapshot>/<category-slug>.parquet`.

`planet_parquet`: build once via `oex-cli osm-build-cache --planet`,
then any country export is a fast read against the Hilbert-sorted parquet
at `<cache_dir>/planet/<snapshot>/<category-slug>.parquet`.

## Pinning a release / snapshot

Both sources resolve to the latest data by default. Pin a specific version
in the country YAML when you need a reproducible run:

```yaml
source:
  overture:
    release: 2026-04-15.0          # default 'latest' -> resolved from S3
  osm:
    snapshot: 2026-05-01           # default 'latest'
```

Resolution rules:

- **Overture `release`**: any literal release like `2026-04-15.0` is used
  verbatim with no lookup. `latest` lists the public S3 bucket and picks the
  highest `YYYY-MM-DD.N`.
- **OSM `snapshot` for `planet_parquet`**: must match an existing snapshot
  directory under `<cache_dir>/planet/`. A missing snapshot is a loud error,
  not a silent fallback. `latest` picks the newest dir.
- **OSM `snapshot` for `geofabrik`**: this is a label for the per-country
  cache dir. Geofabrik only publishes `*-latest.osm.pbf` URLs (no historical
  archive), so a fresh build always pulls today's PBF regardless of the
  label. To truly pin an OSM date, either use `planet_parquet` with a
  pre-built snapshot, or build the geofabrik cache from your own historical
  PBF: `oex-cli osm-build-cache --pbf <historical.osm.pbf>`.

The resolved version is logged before any per-category work, e.g.:

```text
Overture source: release=2026-04-15.0 bucket=overturemaps-us-west-2
OSM source: geofabrik IND, snapshot=2026-05-07, cache=...
```

It also lands inside every zip's `README.txt` (Source, Snapshot fields).

## HDX publication

HDX push is **off by default**. Enable per run:

```yaml
hdx:
  push: true
  site: prod                     # or 'demo'
  api_key: ${oc.env:HDX_API_KEY}
  owner_org: your-org
  maintainer: your-username
  user_agent: my-pipeline/1.0    # optional, defaults to oex
```

Each category supplies its own HDX metadata block:

```yaml
- name: Buildings
  hdx:
    title: Buildings of Nepal
    notes: |
      Building footprints from Overture (OSM + Microsoft + Google + Esri)
      and OpenStreetMap.
    tags: [buildings, geodata]
    license: hdx-odc-odbl                                     # or a free-form license string
    license_url: https://opendatacommons.org/licenses/odbl/1-0/
    caveats: Verified at the community level only.
    dataset_source: OpenStreetMap contributors                # optional override
```

`dataset_source` is the value HDX displays under "Source" on the dataset
page. When unset, the runner supplies a default like
`OpenStreetMap (Geofabrik IND 2026-05-07)` for OSM exports or
`Overture Maps Foundation 2026-04-15.0` for Overture. Override it to match
your organisation's standard (HOT-OSM uses the verbatim string
`OpenStreetMap contributors`).

When both `overture` and `osm` are enabled for a category, both sources
contribute resources to the same HDX dataset (one zip per source per format).
