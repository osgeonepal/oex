# Get started

## Install

Pick one. All install the `oex-cli` command.

### One-line installer

```bash
curl -LsSf https://raw.githubusercontent.com/osgeonepal/oex/main/scripts/install.sh | sh
```

Picks the best installer it finds on your machine: `uv`, then `pipx`, then
`pip --user`. To install a docker wrapper at `/usr/local/bin/oex-cli`
instead:

```bash
curl -LsSf https://raw.githubusercontent.com/osgeonepal/oex/main/scripts/install.sh | sh -s -- --docker
```

### uv

```bash
uv tool install oex
oex-cli osm npl
```

### pipx

```bash
pipx install oex
oex-cli osm npl
```

### pip

```bash
pip install --user oex
oex-cli osm npl
```

### Docker, no install

```bash
docker run --rm -v "$PWD/output:/app/output" \
  ghcr.io/osgeonepal/oex:latest oex-cli osm npl
```

For the short form, set a shell alias once:

```bash
alias oex-cli='docker run --rm \
  -v "$PWD/output:/app/output" \
  -v "$PWD/configs:/app/configs:ro" \
  ghcr.io/osgeonepal/oex:latest oex-cli'
```

After that, `oex-cli osm npl` runs the container.

## Three flows

### 1. Download a country

```bash
oex-cli osm npl
```

Eight bundled categories (Buildings, Roads, Hospitals, Schools, Rivers,
Land Use, Transportation Hubs, Settlements) for Nepal as GeoPackage and
Shapefile zips.

Replace `npl` with any ISO3: `usa`, `bra`, `ind`, `ken`, `ngr`, ...

For Overture Maps instead of OSM:

```bash
oex-cli overture npl
```

### 2. Use a curated category set

Three schemas are bundled in `configs/examples/`. Reference one with
`--config`:

```bash
# HOT-style HDX layers (12 categories matching hotosm_<iso3>_*)
oex-cli osm --config configs/examples/hot-schema.yaml --iso3 NPL

# Full Overture data package (15 datasets, one per theme/feature_type)
oex-cli overture --config configs/examples/overture-package-schema.yaml --iso3 NPL

# Minimal custom example (Hospitals + Healthcare POIs)
oex-cli osm --config configs/examples/custom-schema.yaml --iso3 NPL
```

When running from the docker image, the bundled examples live at
`/app/configs/examples/`.

### 3. Add your own category

Write a YAML:

```yaml
# my-stuff.yaml
iso3: NPL
key: my_stuff
dataset_name: Nepal Coffee Shops
categories:
  - name: Cafes
    osm:
      filter:
        amenity: ["cafe"]
      select:
        - feature_id AS id
        - tags['name'] AS name
        - tags['opening_hours'] AS opening_hours
```

Run it:

```bash
oex-cli osm --config ./my-stuff.yaml
```

See [Custom categories](custom-categories.md) for the full schema reference.

## Where does the data go

```text
output/
└── npl/
    ├── osm/
    │   ├── my_stuff_npl_buildings_gpkg.zip
    │   └── my_stuff_npl_buildings_shp.zip
    └── overture/
        └── ...
```

Each zip contains the GIS file(s) plus `README.txt`, `config.yaml`, and
optionally `metadata.json` (when `output.metadata: true`) with per-column
null shares, distinct counts, geometry types, and bbox.

## OSM engine: geofabrik vs planet

Two engines feed the OSM exporter. The right one depends on how you want
country PBFs to land on disk.

- `engine: geofabrik` (default). Downloads the per-country PBF from
  Geofabrik's mirror (~30 to 200 MB depending on country). One run per
  country pays a small download. Snapshot date is whatever Geofabrik
  publishes that day.

- `engine: planet`. Clips the country PBF out of a local planet PBF
  (~87 GB) using `osmium extract --strategy=complete_ways`, then runs
  quackosm once with the union of all category tag filters. Picks every
  feature the schema cares about in a single PBF parse, no per-category
  reparse. Useful when you control the snapshot date or are sweeping
  many countries from one local planet.

  Requires `osmium-tool` on PATH:

  ```bash
  # Fedora / RHEL
  sudo dnf install osmium-tool

  # Debian / Ubuntu
  sudo apt install osmium-tool

  # macOS
  brew install osmium-tool
  ```

  And a planet PBF locally; download once with:

  ```bash
  oex-cli osm-build-cache
  ```

  Point your config at it:

  ```yaml
  source:
    osm:
      engine: planet
      pbf_path: /path/to/planet-latest.osm.pbf
  ```

  Or, to prefer geofabrik but fall back to planet for countries Geofabrik
  doesn't publish (e.g. some small territories):

  ```yaml
  source:
    osm:
      engine: geofabrik
      planet_fallback: true
      pbf_path: /path/to/planet-latest.osm.pbf
  ```

  By default a missing `pbf_path` is a loud failure. To download the
  planet PBF the first time the planet path is needed, opt in:

  ```yaml
  source:
    osm:
      auto_download_planet: true
  ```

  or pass `--download-if-missing` on the CLI.

## Develop from source

```bash
git clone https://github.com/osgeonepal/oex
cd oex
just setup
just test
just osm nepal
```
