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
    enabled: true                  # the map lives on the report page
  pmtiles:
    enabled: true
    max_zoom: 14                   # sharper when zoomed in, larger archive
  s3:
    enabled: true                  # required for the map to render (see below)
```

Both report pages then carry a map below their table, with a checkbox per layer,
feature counts in the legend, and a popup on click. Per-category datasets get one
tileset per source; `hdx.combine` merges every layer into one tileset carrying
`category` and `source`.

Both pages measure quality the same way: how many of a layer's attribute columns
carry data (green at 50% of rows or more, amber partial, red rare under 25%) plus
the share of features named. The combined table shows one bar per layer; the
per-category report breaks it down column by column.

The combined tileset is built from per-layer GeoParquet, which is written locally
and, with `output.s3.enabled`, staged to `{prefix}/{ISO3}/_layers/{source}/{slug}.parquet`.
That staged copy is what lets a later run of the other source put both sources on
one map. Add `geoparquet` to `output.formats` to keep it as a deliverable.

Colours and map assets are configurable under `output.report`: `palette`, and
`map_assets` (`basemap_tiles`, `maplibre_js`, `pmtiles_js`). The published page
fetches those at view time, so repoint them to pin a version or to serve them from
your own host.

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
Overture run uploads to your bucket and still reads Overture anonymously. PMTiles
come from the GDAL build inside duckdb, so no extra tools are needed.

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

## Running without HDX

Every artifact is written to `output/<iso3>/<source>/` whether or not you push.
With `hdx.push: false` you still get the format zips, the per-layer GeoParquet,
each layer's `metadata.json` and report page, the tilesets, and, under
`hdx.combine`, the merged tileset plus the `_overview.html` landing page.

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
