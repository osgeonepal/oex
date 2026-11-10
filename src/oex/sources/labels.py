"""How each source is named in output, so every place that shows it agrees."""

# (short, long) per source. Short names go where space is tight, such as resource and
# layer names; long names go where the reader needs the full provenance.
SOURCE_LABELS = {
    "osm": ("OSM", "OpenStreetMap"),
    "overture": ("Overture", "Overture"),
    "file": ("Supplied", "Supplied data file"),
}


# The sources a config turns on and off with `source.<name>.enabled`, in run order.
RUN_SOURCES = ("osm", "overture")


def short_label(name: str) -> str:
    return SOURCE_LABELS.get(name, (name.title(), name.title()))[0]


def long_label(name: str) -> str:
    return SOURCE_LABELS.get(name, (name.title(), name.title()))[1]
