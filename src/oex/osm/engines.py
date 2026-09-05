"""The OSM source engines, named once so config validation and dispatch agree."""

ENGINE_NAMES = ("geofabrik", "planet", "postpass", "rawdata")

# Engines that query a live database rather than reading a PBF snapshot.
LIVE_ENGINE_NAMES = ("postpass", "rawdata")
