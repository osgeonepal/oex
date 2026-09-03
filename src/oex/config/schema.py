"""Typed run configuration."""

from dataclasses import dataclass, field
from typing import Any

from oex.palette import DEFAULT_PALETTE

OsmTagFilter = dict[str, Any]


@dataclass
class CombinedHdx:
    """Metadata for the one dataset that `hdx.combine` publishes every category onto.

    Mirrors a category's `hdx:` block, so dataset-level metadata is described the
    same way per-layer metadata is. Every value is optional: an empty one falls
    back to what oex derives from the categories and the source that built them.
    """

    # Dataset slug. Empty falls back to "{key}_{iso3}".
    name: str = ""
    # Supports {country} and {iso3}.
    title: str = ""
    notes: str = ""
    caveats: str = ""
    source: str = ""
    tags: list[str] = field(default_factory=list)


@dataclass
class HdxConfig:
    push: bool = False
    site: str = "demo"
    api_key: str | None = None
    owner_org: str | None = None
    maintainer: str | None = None
    user_agent: str = "oex"
    methodology: str = "Other"
    methodology_other: str = "Open Source Geographic information"
    # Supports {country}, {category}, {iso3}. Empty falls back to
    # "<category> of <iso3>".
    title_template: str = ""
    # Destructive: deletes every existing resource on the dataset before upload.
    purge_existing_resources: bool = False
    # Publish every category as resources on ONE HDX dataset instead of one
    # dataset per category.
    combine: bool = False
    combined: CombinedHdx = field(default_factory=CombinedHdx)


@dataclass
class DuckdbConfig:
    # 8 retries / 500 ms initial / 2x backoff and a 120 s timeout absorb
    # transient S3 blips so a 200-country batch doesn't abort on one shard.
    http_retries: int = 8
    http_retry_wait_ms: int = 500
    http_retry_backoff: float = 2.0
    http_timeout_ms: int = 120_000
    temp_dir: str = "/tmp/duckdb_temp"


@dataclass
class LoggingConfig:
    level: str = "INFO"
    fmt: str | None = None


@dataclass
class MapAssetsConfig:
    """Where the report map gets its basemap and its JavaScript.

    The published page fetches these at view time, so they are configurable: pin a
    different version, or serve them from your own host when a CDN is unreachable.
    """

    basemap_tiles: str = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
    basemap_attribution: str = "(c) OpenStreetMap contributors"
    maplibre_css: str = "https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.css"
    maplibre_js: str = "https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.js"
    pmtiles_js: str = "https://unpkg.com/pmtiles@3.2.1/dist/pmtiles.js"


@dataclass
class ReportConfig:
    enabled: bool = False
    # Layer colours, assigned in layer order and cycled when there are more layers.
    palette: list[str] = field(default_factory=lambda: list(DEFAULT_PALETTE))
    map_assets: MapAssetsConfig = field(default_factory=MapAssetsConfig)


@dataclass
class PmtilesConfig:
    # Higher max_zoom means sharper detail when zoomed in, at the cost of archive size.
    enabled: bool = False
    min_zoom: int = 0
    max_zoom: int = 12


@dataclass
class S3Config:
    enabled: bool = False
    bucket: str = ""
    prefix: str = ""
    region: str = ""
    acl: str = "public-read"
    endpoint_url: str | None = None
    # Mid-path folder and filename id segment. Empty falls back to iso3.
    folder: str = ""
    # Keep the {category}/ subfolder in the key. False writes a flat layout.
    nest_by_category: bool = True
    # Keep the source token (osm/overture) in the artifact filename.
    name_include_source: bool = True
    # Requires output.split_by_geometry.
    nest_by_geometry: bool = False


@dataclass
class OutputConfig:
    dir: str = "output"
    formats: list[str] = field(default_factory=lambda: ["gpkg", "shp"])
    metadata: bool = False
    report: ReportConfig = field(default_factory=ReportConfig)
    pmtiles: PmtilesConfig = field(default_factory=PmtilesConfig)
    s3: S3Config = field(default_factory=S3Config)
    resume: bool = True
    remove_after_upload: bool = True
    # Labels are points, lines, polygons.
    split_by_geometry: bool = False


@dataclass
class ParallelConfig:
    enabled: bool = True
    threads: int | None = None
    memory_gb: int | None = None


@dataclass
class BoundaryConfig:
    geom: str | None = None
    geoboundaries_release: str = "CGAZ"
    geoboundaries_level: str = "ADM0"
    # Optional outward buffer applied to the resolved boundary.
    # The geometry is reprojected to EPSG:3857, buffered by this many metres,
    # then reprojected back to EPSG:4326. 0 = no buffer.
    buffer_meters: float = 0.0


@dataclass
class OvertureSourceConfig:
    enabled: bool = True
    engine: str = "duckdb"
    release: str = "latest"
    s3_region: str = "us-west-2"
    s3_bucket: str = "overturemaps-us-west-2"


@dataclass
class OsmSourceConfig:
    enabled: bool = True
    engine: str = "geofabrik"
    cache_dir: str = "data/osm"
    snapshot: str = "latest"
    keep_pbf: bool = False
    pbf_url: str = "https://planet.openstreetmap.org/pbf/planet-latest.osm.pbf"
    md5_url: str = "https://planet.openstreetmap.org/pbf/planet-latest.osm.pbf.md5"
    geofabrik_index_url: str = "https://download.geofabrik.de/index-v1.json"
    geofabrik_clip_to_boundary: bool = True
    planet_clip_to_boundary: bool = True
    pbf_path: str | None = None
    planet_fallback: bool = False
    auto_download_planet: bool = False
    # Postpass is a shared Geofabrik service, so the engine refuses areas above
    # this rather than letting a country-sized boundary through by accident.
    postpass_endpoint: str = "https://postpass.geofabrik.de/api/interpreter"
    postpass_max_area_sq_km: float = 2000.0
    rawdata_endpoint: str = "https://api-prod.raw-data.hotosm.org/v1"
    # Engine to try when `engine` fails, so one upstream outage does not stop a run.
    fallback_engine: str = ""


@dataclass
class PcodesSourceConfig:
    enabled: bool = False
    cache_dir: str = "data/pcodes"
    levels: list[int] = field(default_factory=lambda: [1, 2, 3, 4])
    manifest_url: str = "https://data.fieldmaps.io/edge-matched.json"
    parquet_url_template: str = (
        "https://data.fieldmaps.io/edge-matched/humanitarian/intl/adm{level}_polygons.parquet"
    )
    manifest_group: str = "humanitarian"
    # 'geos' (default): precise ST_Contains; correct to the metre. 'h3_neighbor': 1-ring
    # hash fallback, ~5 km error at admin borders, memory-bounded. Set h3_neighbor on
    # high-cardinality categories (buildings, roads, waterways) to avoid OOM on big
    # countries; small categories keep geos by inheritance.
    boundary_resolution: str = "geos"


# HDX licence ids are identifiers, not titles; reports show the title instead.
HDX_LICENSE_LABELS = {
    "hdx-odc-odbl": "Open Database License (ODC-ODbL)",
    "hdx-odc-by": "Open Data Commons Attribution License",
    "hdx-odc-pddl": "Open Data Commons Public Domain Dedication and License",
    "cc-by": "Creative Commons Attribution 4.0",
    "cc-by-sa": "Creative Commons Attribution Share-Alike 4.0",
    "cc-by-igo": "Creative Commons Attribution for Intergovernmental Organisations",
    "cc-zero": "Creative Commons Zero (Public Domain)",
    "cc-by-nc": "Creative Commons Attribution Non-Commercial",
    "public-domain": "Public Domain",
}


def license_label(value: str) -> str:
    """Human-readable licence name, falling back to whatever the config set."""
    return HDX_LICENSE_LABELS.get(value, value)


@dataclass
class CategoryHdx:
    title: str | None = None
    notes: str = "Vector data export."
    # Each resource's description; falls back to the opening of `notes` when unset.
    summary: str = ""
    tags: list[str] = field(default_factory=lambda: ["geodata"])
    license: str = "hdx-odc-odbl"
    license_url: str | None = None
    caveats: str = (
        "Data may contain errors. Verified at the community level only; "
        "individual features may need correction."
    )
    # HDX dataset_source override. When unset, defaults to "OpenStreetMap" or "Overture".
    dataset_source: str | None = None


@dataclass
class CategoryOverture:
    enabled: bool = True
    theme: str = ""
    feature_type: str = ""
    select: list[str] = field(default_factory=list)
    where: list[str] = field(default_factory=list)
    tiles: bool = True


@dataclass
class CategoryOsm:
    # `filter` is the quackosm tag filter applied at parquet BUILD time.
    # `where` is SQL applied at QUERY time over the already-built parquet.
    enabled: bool = True
    select: list[str] = field(default_factory=list)
    where: list[str] = field(default_factory=list)
    filter: OsmTagFilter = field(default_factory=dict)
    tiles: bool = True


@dataclass
class TransliterateRule:
    target: str = ""
    source: str = ""
    prefer: str | None = None


@dataclass
class CategoryTemporal:
    # Column alias in the materialised table that carries a per-feature
    # timestamp. When set and present, min/max drive HDX dataset_date and the
    # report's temporal block. Must be reachable via the source's select.
    column: str | None = None


@dataclass
class CategoryConfig:
    name: str = ""
    formats: list[str] | None = None
    skip_pcodes: bool = False
    # Override source.pcodes.boundary_resolution per category. None inherits.
    boundary_resolution: str | None = None
    hdx: CategoryHdx = field(default_factory=CategoryHdx)
    overture: CategoryOverture = field(default_factory=CategoryOverture)
    osm: CategoryOsm = field(default_factory=CategoryOsm)
    transliterate: list[TransliterateRule] = field(default_factory=list)
    temporal: CategoryTemporal = field(default_factory=CategoryTemporal)


@dataclass
class RootConfig:
    iso3: str = ""
    key: str = ""
    dataset_name: str | None = None
    subnational: bool = False
    frequency: str = "yearly"
    categories_file: str | None = None
    boundary: BoundaryConfig = field(default_factory=BoundaryConfig)
    output: OutputConfig = field(default_factory=OutputConfig)
    parallel: ParallelConfig = field(default_factory=ParallelConfig)
    duckdb: DuckdbConfig = field(default_factory=DuckdbConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    hdx: HdxConfig = field(default_factory=HdxConfig)
    source: dict[str, Any] = field(
        default_factory=lambda: {
            "overture": OvertureSourceConfig(),
            "osm": OsmSourceConfig(),
            "pcodes": PcodesSourceConfig(),
        }
    )
    categories: list[CategoryConfig] = field(default_factory=list)


def dataset_identity(cfg: RootConfig) -> str:
    """Path and filename id for a dataset: iso3 when set, else the S3 folder id.

    Sub-national exports (Tasking Manager projects) have no country code; they
    identify by project id through ``output.s3.folder``.
    """
    return cfg.iso3.lower() or cfg.output.s3.folder
