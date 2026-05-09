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

## Production sanity

- Always run with `hdx.site: demo` first against the HDX demo instance
  before pointing at `prod`.
- `oex` writes the zips to disk regardless of `hdx.push`, so a failed
  upload never costs you the export work.
