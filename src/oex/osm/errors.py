"""Errors shared by the OSM source engines."""


class OsmEngineUnavailableError(RuntimeError):
    """An engine's upstream could not be reached or refused the request; kept apart from bad
    data so a fallback covers an outage without masking a defect."""
