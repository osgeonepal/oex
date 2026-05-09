# Custom categories

A category is one logical dataset. Each one is written out as a zip per
output format (`gpkg`, `shp`, `geojson`, `kml`). You write its SELECT and
WHERE in plain SQL.

## Anatomy

```yaml
- name: Buildings
  formats: [gpkg, shp, kml]         # optional override of output.formats
  hdx:
    title: Buildings of Nepal
    notes: ...
    tags: [buildings, geodata]
    license: ODbL 1.0
  overture:
    enabled: true
    theme: buildings
    feature_type: building
    select:
      - id
      - names.primary AS name
      - height
      - num_floors
    where: []
  osm:
    enabled: true
    filter:
      building: true
    select:
      - feature_id AS id
      - tags['name'] AS name
      - tags['height'] AS height
    where: []
```

When both `overture.enabled` and `osm.enabled` are true, both sources
contribute resources to the same HDX dataset.

## Mental model

| Field           | Where it runs                     | Purpose                          |
| --------------- | --------------------------------- | -------------------------------- |
| `osm.filter`    | quackosm (PBF -> parquet)         | which OSM features enter cache   |
| `osm.select`    | DuckDB SQL on cached parquet      | columns to keep                  |
| `osm.where`     | DuckDB SQL on cached parquet      | extra row filter (rarely needed) |
| `overture.select` | DuckDB SQL on Overture S3 read  | columns to keep                  |
| `overture.where`  | DuckDB SQL on Overture S3 read  | row filter                       |

## OSM source schema

quackosm produces parquet with three columns:

- `feature_id` (varchar): OSM type + id, e.g. `node/12345`
- `tags` (`MAP<VARCHAR, VARCHAR>`): all retained tags
- `geometry`

`tags['key']` returns the scalar value (or NULL when the tag is absent).
See the [OSM Map Features wiki](https://wiki.openstreetmap.org/wiki/Map_features)
for available keys.

`osm.filter` accepts the quackosm tag-filter shape:

```yaml
osm:
  filter:
    building: true                          # any value
    highway: ["primary", "secondary"]       # only these values
    amenity: ["hospital", "clinic"]
```

## Overture source schema

Overture data lives at `s3://overturemaps-us-west-2/release/<release>/theme=<theme>/type=<feature_type>/`.

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
| `divisions`      | `division`          | `id`, `names`, `subtype`, `country`, `region`, `population`              |
| `divisions`      | `division_area`     | `id`, `names`, `subtype`, `country`, `region`                            |
| `divisions`      | `division_boundary` | `id`, `subtype`, `class`                                                 |
| `places`         | `place`             | `id`, `names`, `categories`, `addresses`, `phones`, `websites`           |
| `transportation` | `connector`         | `id`                                                                     |
| `transportation` | `segment`           | `id`, `names`, `class`, `subclass`, `subtype`, `road_surface`            |

Authoritative reference: [overturemaps.org/schema](https://docs.overturemaps.org/schema/).

Overture renames types across releases (e.g. `boundary` -> `division_boundary`
in 2026-04-15.0). Pin `source.overture.release` if you depend on a specific
schema.

## Three ways to plug in a category set

1. **Inline in the country YAML.** Drop a `categories:` block; it replaces
   the bundled defaults wholesale.
2. **Reference a separate file** with `categories_file: path/to/schema.yaml`.
   That YAML must contain a top-level `categories:` list.
3. **Both.** `categories_file` loads the base set; an inline `categories:`
   block then overrides for that country.

## Bundled schemas

| File                                  | What it produces                                |
| ------------------------------------- | ----------------------------------------------- |
| `configs/examples/hot-schema.yaml`            | 11 layers matching HOT's HDX exports     |
| `configs/examples/overture-package-schema.yaml` | 15 datasets, one per Overture theme/type |
| `configs/examples/custom-schema.yaml`         | Minimal 2-category example               |

The bundled defaults at `src/oex/defaults/base.yaml` ship the eight-theme
combo (Buildings, Roads, Hospitals, Schools, Rivers, Land Use,
Transportation Hubs, Settlements) drawn from both sources.

## Optional per-category features

```yaml
- name: Buildings
  transliterate:
    - target: name_latin
      source: name
      prefer: name_en
```

`transliterate` adds a Latin-script display column. When `prefer` is non-null
it is used verbatim; otherwise `source` is transliterated via `unidecode`.

## Optional run-level features

```yaml
output:
  report:
    enabled: true            # writes report.html per category and (if hdx.push)
                             # attaches it as the dataset's interactive view

source:
  pcodes:
    enabled: true            # adds adm{N}_pcode and adm{N}_name columns
                             # from fieldmaps.io edge-matched humanitarian

hdx:
  purge_existing_resources: true   # destructive: clears the dataset before
                                   # uploading fresh resources
```
