# HDX publication

HDX push is **off by default**. You can use `oex` purely as a downloader.

## Enable

```yaml
hdx:
  push: true
  site: prod                           # or 'demo'
  api_key: ${oc.env:HDX_API_KEY}
  owner_org: your-org-slug
  maintainer: your-username
```

Or per-run on the CLI:

```bash
oex-cli osm npl --hdx-push
```

Credentials can come from environment variables instead of the YAML:

```bash
export HDX_API_KEY=...
export HDX_OWNER_ORG=...
export HDX_MAINTAINER=...
oex-cli osm npl --hdx-push
```

Give env interpolations a default so the config loads when the variable is
absent, which is what lets one file run both with and without pushing:

```yaml
hdx:
  api_key: ${oc.env:HDX_API_KEY,null}
```

## What happens

For each category that succeeds:

1. The HDX dataset is created or updated under `<key>_<iso3>_<category>`.
2. Each format zip is uploaded as a resource (`.gpkg.zip`, `.shp.zip`,
   `.geojson.zip`).
3. The dataset's time period is set to the source snapshot date.

Per-category metadata comes from each category's `hdx:` block:

```yaml
- name: Buildings
  hdx:
    title: Buildings of Nepal
    notes: |
      Building footprints from OSM and Overture.
    tags: [buildings, geodata]
    license: ODbL 1.0                           # or 'hdx-odc-odbl' for the canonical id
    license_url: https://opendatacommons.org/licenses/odbl/1-0/
    caveats: Verified at the community level only.
```

## Cleanup before upload

```yaml
hdx:
  purge_existing_resources: true   # destructive: clears the dataset before upload
```

Or per-run: `oex-cli osm npl --hdx-push --hdx-purge`.

## Hosting resources on S3 instead of HDX

```yaml
output:
  s3:
    enabled: true
    bucket: my-bucket          # or OEX_S3_BUCKET
    prefix: hotosm/exports     # or OEX_S3_PREFIX
    region: us-east-1          # or OEX_S3_REGION
    acl: public-read           # so HDX can fetch the URL
    endpoint_url: null         # set for R2/MinIO via OEX_S3_ENDPOINT_URL
```

Each artifact uploads to `s3://<bucket>/<prefix>/<iso3>/<category>/<filename>`,
then attaches to HDX as a URL link instead of an upload.

### Shaping the key

For consumers that expect a fixed layout, these reshape the path:

```yaml
output:
  split_by_geometry: true      # one artifact per points/lines/polygons, not one per format
  s3:
    folder: hotosm_project_1   # replaces the iso3 segment, and the filename id
    nest_by_category: true     # keep the {category}/ segment
    nest_by_geometry: true     # add a {geometry}/ segment under it
    name_include_source: false # drop the osm/overture token from the filename
```

With an empty `key`, so the folder id carries the whole dataset prefix, that gives
the Tasking Manager layout:

```
TM/hotosm_project_1/buildings/polygons/hotosm_project_1_buildings_polygons_shp.zip
```

`nest_by_geometry` needs `split_by_geometry`. Without it one artifact holds every
geometry type and cannot sit under a single segment, so the run fails rather than
writing a misleading key.

AWS credentials come from boto3's default chain: `AWS_ACCESS_KEY_ID` +
`AWS_SECRET_ACCESS_KEY` (with optional `AWS_SESSION_TOKEN`), `AWS_PROFILE`,
or an IAM role on EC2. Nothing oex-specific needed for credentials.

## Optional features

```yaml
output:
  report:
    enabled: true        # interactive HTML report attached as customviz

source:
  pcodes:
    enabled: true        # adds adm{N}_pcode and adm{N}_name columns

categories:
  - name: Buildings
    transliterate:
      - target: name_latin
        source: name
        prefer: name_en  # used as-is when not null, else transliterated
```

See [Custom categories](custom-categories.md) for the per-category schema.

## One dataset for all categories

By default each category becomes its own HDX dataset (`{key}_{iso3}_{slug}`).
`hdx.combine` publishes every category onto one dataset instead.

```yaml
hdx:
  push: true
  combine: true
  combined:                        # same field names as a category's hdx: block
    name: hot_eq_ven               # slug; empty falls back to {key}_{iso3}
    title: "{country} - M 7.5 Earthquake - June 2026"   # {country}, {iso3}
    notes: Data from the HOTOSM response to the June 2026 earthquake.
    caveats: Overture footprints are partly AI-generated.
    source: OpenStreetMap contributors; Overture Maps
    tags: [earthquake-tsunami, natural disasters]
```

Every value under `combined` is optional and falls back to what oex derives from
the categories. Crisis tags can only be applied by an HDX sysadmin: publishing
with one rejects the whole dataset.

Layers appear in `categories` order. Running both sources into the same dataset
**accumulates**: the Overture run adds to what the OSM run published rather than
replacing it. `hdx.purge_existing_resources` clears the dataset first instead.

`oex-cli all` runs every source the config enables in one command, OSM then
Overture, into the same dataset:

```bash
oex-cli all COD --config configs/hot-ce-cod.yaml
```

It reads `source.osm.enabled` and `source.overture.enabled` and runs each in
order, so the accumulation above happens from a single invocation. Running the
`osm` and `overture` subcommands separately does the same thing in two steps.

## Vector tiles (PMTiles)

```yaml
output:
  report:
    enabled: true
  pmtiles:
    enabled: true
    max_zoom: 14
    max_extra_zooms: 4
  s3:
    enabled: true
```

The map lives on the report page, and it needs S3 to render, for the reason given
below. Tilesets are built with [tippecanoe](https://github.com/felt/tippecanoe) 2.31
or later, which has to be on `PATH`. Debian bookworm-backports, Debian trixie and
Ubuntu 24.04 package it as `tippecanoe`. A run with PMTiles enabled checks for it
before exporting anything. Every feature, however small, is kept at the top zoom,
and tippecanoe adds zooms past `max_zoom` until one holds every feature, up to
`max_extra_zooms` (default 4) more, and 0 turns this off. Each added zoom makes the
archive larger. Past that limit tippecanoe drops the densest features from the top zoom. A category's
tileset has one layer named for the category, such as `buildings`, so one map style serves
every country's tiles.

The page carries a Data view, one panel per layer with its columns broken down one
by one, and, when a tileset exists, a Map view behind the same switcher. The map has
a checkbox per layer, select all and clear all, feature counts in the legend, a hover
card naming the feature under the pointer and a popup of its attributes on click.
Per-category datasets get one tileset per source; `hdx.combine` merges every layer
into one tileset carrying `category` and `source`.

Attribute quality is measured as how many of a layer's columns carry data (green at
50% of rows or more, amber partial, red rare under 25%) plus the share of features
named.

The combined tileset is built from per-layer GeoParquet, which is written locally
and, with `output.s3.enabled`, staged to `{prefix}/{ISO3}/_layers/{source}/{slug}.parquet`.
That staged copy is what lets a later run of the other source put both sources on
one map.

Adding `geoparquet` to `output.formats` also publishes each layer as an HDX resource
pointing at that staged object, unzipped, so a client can range-read it over HTTP.
It needs `output.s3.enabled`, since the resource links the staged object rather than
uploading a second copy; a config that asks for it with S3 off and `hdx.push` on is
refused rather than quietly skipped.

Each file is GeoParquet 1.1 with a `bbox` covering column, and its features are
ordered along a Hilbert curve over the layer's own extent. Each row group therefore
covers a small area, and its bbox statistics tell a reader which groups a query can
skip. The number of row groups follows the layer size, so a layer of three features
and one of three million both split sensibly. The `bbox` column is storage, not an
attribute: it is left out of the tilesets and of the column list in the layer
metadata.

Colours and map assets are configurable under `output.report`. `theme` sets
`accent_color` (buttons, bars, the selected layer), `map_popup_color` and
`boundary_outline_color`. `accent_color` and `map_popup_color` are refused if either
falls below 4.5:1 against white: the download button draws white text on the accent,
and the map popup draws its heading in the popup colour on white. `palette` colours the
layers themselves, and `map_assets` (`basemap_tiles`, `maplibre_js`, `pmtiles_js`)
is fetched at view time, so repoint it to pin a version or serve it from your own
host.

`related_datasets: true` adds a line under the title of each per-category page linking the
area's other category datasets on HDX. It needs `hdx.push`, since without it no dataset page
is published to link to. The About section of every page links the HDX dataset the layer is
published to, and each layer's STAC Item carries the same link as `rel="via"`. With
`related_datasets` on, each per-category STAC Collection also links the area's other category
Collections as `rel="related"`.

`output.stac.catalog: true` keeps a STAC catalog tree on S3 up to date. Each run links the
Collections it published into an area catalog at `{prefix}/{folder or ISO3}/catalog.json` and
links that area into a root catalog at `{prefix}/catalog.json`, creating either when missing.
`output.stac.id`, `title` and `description` name the root. `output.stac.group`, such as
`activations`, puts a catalog of that name between the root and the area, so related areas
are listed together; `group_title` names it. Writes are conditional, so runs
updating the same catalog at once keep each other's links. Links are added or refreshed,
never removed.

### The map needs S3 and a bucket CORS policy

The table always renders. The map fetches tiles with cross-origin **range**
requests, and HDX's file store redirects them to S3 with a CORS header scoped to
the HDX origin, so an HDX-hosted tileset stays blank. Set `output.s3.enabled` so
the resource URL points at your bucket, and give the bucket:

```json
[{"AllowedOrigins": ["*"], "AllowedMethods": ["GET", "HEAD"],
  "AllowedHeaders": ["*"], "ExposeHeaders": ["range", "etag"]}]
```

The object ACL says who may read; CORS is separate and is set per bucket, not per
object. Both are needed.

Overture stays readable even when AWS credentials are present, so an S3-enabled
Overture run uploads to your bucket and still reads Overture anonymously.

CLI flags mirror the config: `--hdx-combine/--no-hdx-combine`,
`--pmtiles/--no-pmtiles`, and `--s3/--no-s3`.

## Local language columns

For OSM exports, oex auto-injects `tags['name:<lang>'] AS name_<lang>` into
each category's select for the country's primary non-English official
languages (up to three). The languages are resolved from the config's
`iso3` via [babel](https://babel.pocoo.org/) (`get_official_languages`)
plus pycountry. Examples:

|ISO3|Injected columns|
|----|----------------|
|NPL|`name_ne`|
|SDN|`name_ar`|
|IND|`name_hi`|
|CHE|`name_de`, `name_fr`, `name_it`|
|USA|(none, English is already in `name_en`)|

Per-category YAML can still pin or override a language by including
`tags['name:<lang>'] AS name_<lang>` explicitly in `osm.select`.

## Layer metadata

The layer metadata resource is a STAC Collection. It names every layer with a
`rel="item"` link, and each Item is its own document beside it, carrying the layer's
footprint, its snapshot time, and the row count and column schema through the
[Table extension](https://stac-extensions.github.io/table/), plus every artifact the
layer publishes as an asset:

```json
{
  "type": "Collection",
  "id": "hot_flood_npl",
  "links": [
    {"rel": "self", "href": "https://.../combined/hot_flood_npl_osm_metadata.json"},
    {"rel": "via", "href": "https://data.humdata.org/dataset/hot_flood_npl"},
    {"rel": "item", "href": "https://.../buildings/hot_flood_npl_buildings_osm_item.json"}
  ],
  "item_assets": {
    "gpkg": {"title": "GeoPackage", "type": "application/zip", "roles": ["data"]}
  }
}
```

```json
{
  "type": "Feature",
  "id": "hot_flood_npl_buildings-osm",
  "collection": "hot_flood_npl",
  "properties": {
    "title": "Buildings",
    "license": "ODbL-1.0",
    "proj:code": "EPSG:4326",
    "table:row_count": 20403,
    "table:columns": [{"name": "id", "type": "VARCHAR"}],
    "oex:snapshot": "2026-09-18T03:55:55Z"
  },
  "assets": {
    "gpkg": {
      "href": "https://.../hot_flood_npl_buildings_osm_gpkg.zip",
      "file:size": 2079637
    }
  }
}
```

An Item id is its layer's own dataset name and the source that built it, so
`hotosm_npl_buildings-osm` whether or not that layer is published on a combined
dataset. Asset hrefs come from the S3 key layout, so ids
and addresses hold from run to run and two datasets compare layer by layer. With
`output.split_by_geometry`, the key carries the partition: `polygons_shp`, `lines_shp`.

Item files are uploaded beside their layer's artifacts and are not listed as HDX
resources, so a dataset gains one metadata resource. Every
dataset shape produces the same pair, including a country export that carries a single
layer, so one dataset link always resolves to one Collection.

`table:columns` gives each column its native type. Statistics that STAC has no field
for, such as null counts and top values, sit beside them under `oex:` keys, and are
what oex's own report reads back. Treat them as internal: the layer's GeoParquet is
the source to query for statistics.

The Collection points at its own published address with `rel="self"` and back at the
HDX dataset page with `rel="via"`. Under `hdx.combine` the merged tileset is a
Collection asset; otherwise each layer's tiles are an Item asset.

Without S3 the hrefs stay relative, so the output directory is a catalog that can be
served from anywhere. A self link needs an absolute address, so a local run writes
none.

Set `output.metadata: true` to write the Collection and its Items. Turning
`output.report.enabled` on implies it, because the report is rendered from them.
Publishing them needs `output.s3.enabled`, since the Collection links each Item by
URL and the Items are uploaded to the bucket; a config that asks for both with S3 off
and `hdx.push` on is refused before the run starts.

## Running without HDX

Every artifact is written to `output/<iso3>/<source>/` whether or not you push.
With `hdx.push: false` you still get the format zips, the per-layer GeoParquet,
each layer's `_item.json`, the Collection in `_metadata.json`, the tilesets, and,
under `hdx.combine`, the merged tileset plus the `_overview.html` page.

```bash
oex-cli osm VEN --config examples/hot-eq-ven.yaml --no-hdx-push --no-s3
```

The pages reference their tileset by filename, so serve the output directory to
view them. The server has to answer range requests with `206 Partial Content`,
which is how PMTiles reads a slice of the archive instead of all of it. Python's
`http.server` answers `200` with the whole file and the map stays blank:

```bash
cd output/ven/osm
uvx --from rangehttpserver python -m RangeHTTPServer 8000
```

Opening the HTML straight off disk leaves the map blank too, because browsers
refuse the cross-origin requests PMTiles makes on `file://`. The table renders
either way.

The local overview describes the layers that run produced. The published one
describes every layer on the HDX dataset, including those an earlier run of the
other source put there, which is knowledge only HDX holds.

## Production sanity

- Always run with `hdx.site: demo` first against the HDX demo instance
  before pointing at `prod`.
- `oex` writes the zips to disk regardless of `hdx.push`, so a failed
  upload never costs you the export work.
