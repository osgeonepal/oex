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
# HOT-style HDX layers (11 categories matching hotosm_<iso3>_*)
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
        - tags['name'][1] AS name
        - tags['opening_hours'][1] AS opening_hours
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

## Develop from source

```bash
git clone https://github.com/osgeonepal/oex
cd oex
just setup
just test
just osm nepal
```
