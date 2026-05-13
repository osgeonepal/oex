"""Typed run configuration."""

from dataclasses import dataclass, field
from typing import Any

OsmTagFilter = dict[str, Any]


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


@dataclass
class DuckdbConfig:
    # 8 retries / 500 ms initial / 2x backoff and a 120 s timeout absorb
    # transient S3 blips so a 200-country batch doesn't abort on one shard.
    http_retries: int = 8
    http_retry_wait_ms: int = 500
    http_retry_backoff: float = 2.0
    http_timeout_ms: int = 120_000
    temp_dir: str = "/tmp/duckdb_temp"
    enable_object_cache: bool = True


@dataclass
class LoggingConfig:
    level: str = "INFO"
    fmt: str | None = None


@dataclass
class ReportConfig:
    enabled: bool = False


@dataclass
class S3Config:
    enabled: bool = False
    bucket: str = ""
    prefix: str = ""
    region: str = ""
    acl: str = "public-read"
    endpoint_url: str | None = None


@dataclass
class OutputConfig:
    dir: str = "output"
    formats: list[str] = field(default_factory=lambda: ["gpkg", "shp"])
    metadata: bool = False
    report: ReportConfig = field(default_factory=ReportConfig)
    s3: S3Config = field(default_factory=S3Config)
    resume: bool = True


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
    pbf_path: str | None = None
    planet_fallback: bool = False
    auto_download_planet: bool = False


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


@dataclass
class CategoryHdx:
    title: str | None = None
    notes: str = "Vector data export."
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


@dataclass
class CategoryOsm:
    # `filter` is the quackosm tag filter applied at parquet BUILD time.
    # `where` is SQL applied at QUERY time over the already-built parquet.
    enabled: bool = True
    select: list[str] = field(default_factory=list)
    where: list[str] = field(default_factory=list)
    filter: OsmTagFilter = field(default_factory=dict)


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
