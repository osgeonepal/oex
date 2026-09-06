"""How each source is named in output, so the four places that show it agree."""

# (short, long) per source. Short names go where space is tight (resource names,
# report tabs); long names go where the reader needs the full provenance.
SOURCE_LABELS = {
    "osm": ("OSM", "OpenStreetMap"),
    "overture": ("Overture", "Overture"),
    "file": ("Supplied", "Supplied data file"),
}


def short_label(name: str) -> str:
    return SOURCE_LABELS.get(name, (name.title(), name.title()))[0]


def long_label(name: str) -> str:
    return SOURCE_LABELS.get(name, (name.title(), name.title()))[1]
