"""Names derived from config values, shared so the exporter and the publisher agree."""

import re
from collections.abc import Iterable

from oex.config.schema import CategoryConfig, RootConfig
from oex.sources.labels import SOURCE_LABELS


def slugify(value: str) -> str:
    """Config text to a filename and HDX-safe token."""
    return re.sub(r"[^a-zA-Z0-9]+", "_", value).lower().strip("_")


def split_artifact_filename(filename: str, known_slugs: Iterable[str] = ()) -> tuple[str, str, str]:
    """(slug, source, format) from a filename without its dataset prefix. The source token is
    optional, so it is matched by name, and `known_slugs` settles a slug like `roads_osm`."""
    if filename.endswith(".zip"):
        rest, _, fmt = filename[: -len(".zip")].rpartition("_")
    else:
        rest, _, fmt = filename.rpartition(".")
    if rest in set(known_slugs):
        return rest, "", fmt
    head, _, maybe_source = rest.rpartition("_")
    if maybe_source in SOURCE_LABELS:
        return head, maybe_source, fmt
    return rest, "", fmt


COMBINED_SLUG = "combined"

# Kept lowercase mid-phrase so "Points of Interest" reads naturally.
_TITLE_LOWER_WORDS = {"a", "an", "and", "at", "by", "for", "in", "of", "on", "or", "the", "to"}

# In the order a reader is offered them. HDX resource names use hdx_publisher.FORMAT_LABEL.
ASSET_TITLES = {
    "gpkg": "GeoPackage",
    "shp": "Shapefile",
    "geojson": "GeoJSON",
    "kml": "KML",
    "fgb": "FlatGeobuf",
    "geoparquet": "GeoParquet",
    "pmtiles": "Vector Tiles",
}


def category_label(category: CategoryConfig) -> str:
    return category.hdx.title or _title_case_category(category.name)


def _title_case_category(name: str) -> str:
    words = name.replace("_", " ").split()
    return " ".join(
        w.lower() if i > 0 and w.lower() in _TITLE_LOWER_WORDS else w.capitalize()
        for i, w in enumerate(words)
    )


def combined_dataset_name(cfg: RootConfig) -> str:
    return cfg.hdx.combined.name or f"{cfg.key}_{cfg.iso3.lower()}"


def resource_prefix(cfg: RootConfig) -> str:
    """Root for every published filename on a dataset: the combined dataset name when
    combining, otherwise ``{key}_{iso3}`` or the S3 folder id."""
    if cfg.hdx.combine:
        return combined_dataset_name(cfg)
    ident = cfg.output.s3.folder or cfg.iso3.lower()
    # An empty key means the folder id is already the whole prefix (hotosm_project_1).
    return f"{cfg.key}_{ident}" if cfg.key else ident


def category_dataset_name(cfg: RootConfig, category: CategoryConfig) -> str:
    """The HDX dataset a category publishes to when categories are not combined."""
    return f"{cfg.key}_{cfg.iso3.lower()}_{slugify(category.name)}"


def layer_dataset_name(cfg: RootConfig, category: CategoryConfig) -> str:
    """Names a layer's Item and its file, alongside the artifacts it describes."""
    return f"{resource_prefix(cfg)}_{slugify(category.name)}"
