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

## Production sanity

- Always run with `hdx.site: demo` first against the HDX demo instance
  before pointing at `prod`.
- `oex` writes the zips to disk regardless of `hdx.push`, so a failed
  upload never costs you the export work.
