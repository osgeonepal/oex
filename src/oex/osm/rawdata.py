"""HOT Raw Data API source engine: live OSM through an asynchronous export job.

The API tracks OSM continuously, so a small area comes back far fresher than a daily
country PBF. A snapshot is submitted, polled until it completes, then downloaded.
"""

import json
import shutil
import time
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import requests

from oex.logging_setup import get_logger
from oex.osm.category_filter import OsmTagsFilter
from oex.osm.country_parquet import write_country_parquet

logger = get_logger(__name__)

DEFAULT_ENDPOINT = "https://api-prod.raw-data.hotosm.org/v1"
DEFAULT_TIMEOUT_SECONDS = 1800
_POLL_SECONDS = 10

# The API reports the OSM primitive per feature; oex identifies features the same way
# quackosm does.
_OSM_TYPE = {
    "nodes": "node",
    "ways_line": "way",
    "ways_poly": "way",
    "relations": "relation",
}


@dataclass(frozen=True)
class RawDataSnapshot:
    parquet: Path
    timestamp: datetime
    label: str
    feature_count: int


def build_filter(tag_filter: OsmTagsFilter) -> dict:
    """Tag filter in the API's `join_or` shape; an empty value list means any value."""
    join_or: dict[str, list[str]] = {}
    for key, value in sorted(tag_filter.items()):
        if value is True:
            join_or[key] = []
        elif isinstance(value, list):
            join_or[key] = list(value)
        elif value:
            join_or[key] = [value]
    if not join_or:
        raise ValueError(
            "rawdata engine needs at least one enabled category with a non-empty osm.filter"
        )
    return {"tags": {"all_geometry": {"join_or": join_or}}}


def _submit(endpoint: str, boundary_geojson: str, tag_filter: OsmTagsFilter, timeout: int) -> str:
    body = {
        "geometry": json.loads(boundary_geojson),
        "filters": build_filter(tag_filter),
        "outputType": "geojson",
        "fileName": "oex",
        # The API defaults this to true, which drops every road and area crossing the
        # boundary. oex clips by intersection, so ask for the same.
        "useStWithin": False,
    }
    response = requests.post(f"{endpoint}/snapshot/", json=body, timeout=timeout)
    if response.status_code != 200:
        raise RuntimeError(
            f"Raw Data API returned HTTP {response.status_code}: {response.text[:500]}"
        )
    task_id = response.json().get("task_id")
    if not task_id:
        raise RuntimeError(
            f"Raw Data API accepted the job but returned no task id: {response.text[:200]}"
        )
    return str(task_id)


def _wait_for(endpoint: str, task_id: str, timeout: int) -> str:
    """Block until the job finishes, returning its download URL."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        response = requests.get(f"{endpoint}/tasks/status/{task_id}/", timeout=120)
        if response.status_code != 200:
            raise RuntimeError(
                f"Raw Data API status returned HTTP {response.status_code}: {response.text[:300]}"
            )
        payload = response.json()
        status = payload.get("status")
        if status == "SUCCESS":
            url = (payload.get("result") or {}).get("download_url")
            if not url:
                raise RuntimeError(f"Raw Data API finished with no download url: {payload}")
            return str(url)
        if status in ("FAILURE", "REVOKED"):
            raise RuntimeError(f"Raw Data API job {task_id} ended as {status}: {payload}")
        time.sleep(_POLL_SECONDS)
    raise RuntimeError(f"Raw Data API job {task_id} did not finish within {timeout}s")


def _download(url: str, work_dir: Path, timeout: int) -> Path:
    archive = work_dir / "snapshot.zip"
    with requests.get(url, stream=True, timeout=timeout) as response:
        response.raise_for_status()
        with archive.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=1 << 20):
                handle.write(chunk)
    with zipfile.ZipFile(archive) as bundle:
        names = [n for n in bundle.namelist() if n.endswith(".geojson") and "clipping" not in n]
        if not names:
            raise RuntimeError(f"Raw Data API archive holds no export geojson: {bundle.namelist()}")
        bundle.extract(names[0], work_dir)
    return work_dir / names[0]


def _rows(path: Path) -> list[tuple[str, str, str]]:
    """Export features as (feature_id, tags_json, wkt), skipping any without geometry."""
    from shapely.geometry import shape as to_shape

    payload = json.loads(path.read_text(encoding="utf-8"))
    rows: list[tuple[str, str, str]] = []
    for feature in payload["features"]:
        geometry = feature.get("geometry")
        properties = feature.get("properties") or {}
        osm_id = properties.get("osm_id")
        kind = _OSM_TYPE.get(properties.get("osm_type") or "", "way")
        if geometry is None or osm_id is None:
            continue
        rows.append(
            (
                f"{kind}/{osm_id}",
                json.dumps(properties.get("tags") or {}, ensure_ascii=False),
                to_shape(geometry).wkt,
            )
        )
    return rows


def fetch_country_parquet(
    *,
    boundary_geojson: str,
    tag_filter: OsmTagsFilter,
    out_path: Path,
    endpoint: str = DEFAULT_ENDPOINT,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> RawDataSnapshot:
    """Run one snapshot job and write its result to country.parquet."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    work_dir = out_path.parent / "_rawdata_work"
    if work_dir.exists():
        shutil.rmtree(work_dir)
    work_dir.mkdir(parents=True)

    task_id = _submit(endpoint, boundary_geojson, tag_filter, timeout=180)
    logger.info("Raw Data API job %s submitted, waiting", task_id)
    url = _wait_for(endpoint, task_id, timeout)
    rows = _rows(_download(url, work_dir, timeout))
    shutil.rmtree(work_dir, ignore_errors=True)

    if not rows:
        raise RuntimeError(
            "Raw Data API returned no features for this boundary and tag filter. Check "
            "boundary.geom and the category osm.filter blocks."
        )
    write_country_parquet(rows, out_path, "Raw Data API")
    stamp = datetime.now(UTC)
    label = stamp.strftime("%Y-%m-%dT%H:%M:%SZ")
    logger.info("Raw Data API snapshot %s: %d features -> %s", label, len(rows), out_path)
    return RawDataSnapshot(parquet=out_path, timestamp=stamp, label=label, feature_count=len(rows))
