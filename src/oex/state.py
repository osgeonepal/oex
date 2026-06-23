"""Per-(country, source) resume state, atomic-write JSON.

A run keeps a single ``.state.json`` per (output_dir, iso3, source) recording,
for each category, when the local build finished and when the HDX upload
completed. With ``output.resume`` enabled the exporter consults this to
skip already-finished work after a partial run, and HDX rate-limit storms
become recoverable without rebuilding zips.

State is keyed by category slug. A snapshot label mismatch (different PBF)
is treated as a miss so a fresh snapshot always rebuilds.
"""

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from oex.logging_setup import get_logger

logger = get_logger(__name__)

_SCHEMA_VERSION = 1


@dataclass
class CategoryState:
    snapshot_label: str = ""
    built_utc: str | None = None
    zip_paths: list[str] = field(default_factory=list)
    metadata_json_path: str | None = None
    uploaded_utc: str | None = None
    hdx_dataset: str | None = None


@dataclass
class SourceState:
    schema_version: int = _SCHEMA_VERSION
    iso3: str = ""
    source: str = ""
    categories: dict[str, CategoryState] = field(default_factory=dict)


class StateStore:
    """Read/write the per-(iso3, source) resume state JSON, atomically."""

    def __init__(self, path: Path, iso3: str, source: str) -> None:
        self._path = path
        self._iso3 = iso3.upper()
        self._source = source
        self._state = self._load()

    def _load(self) -> SourceState:
        if not self._path.is_file():
            return SourceState(iso3=self._iso3, source=self._source)
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("State file %s unreadable, starting fresh: %s", self._path, exc)
            return SourceState(iso3=self._iso3, source=self._source)
        if payload.get("schema_version") != _SCHEMA_VERSION:
            logger.warning(
                "State file %s schema=%s does not match expected %s; starting fresh",
                self._path,
                payload.get("schema_version"),
                _SCHEMA_VERSION,
            )
            return SourceState(iso3=self._iso3, source=self._source)
        if payload.get("iso3") != self._iso3 or payload.get("source") != self._source:
            return SourceState(iso3=self._iso3, source=self._source)
        categories = {
            slug: CategoryState(**entry)
            for slug, entry in (payload.get("categories") or {}).items()
        }
        return SourceState(
            schema_version=_SCHEMA_VERSION,
            iso3=self._iso3,
            source=self._source,
            categories=categories,
        )

    def get(self, slug: str) -> CategoryState | None:
        return self._state.categories.get(slug)

    def reset(self, slug: str) -> None:
        self._state.categories.pop(slug, None)
        self._save()

    def mark_built(
        self,
        slug: str,
        *,
        snapshot_label: str,
        zip_paths: list[Path],
        metadata_json_path: Path | None,
    ) -> None:
        entry = self._state.categories.get(slug) or CategoryState()
        entry.snapshot_label = snapshot_label
        entry.built_utc = _now_iso()
        entry.zip_paths = [str(p.resolve()) for p in zip_paths]
        entry.metadata_json_path = str(metadata_json_path.resolve()) if metadata_json_path else None
        entry.uploaded_utc = None
        entry.hdx_dataset = None
        self._state.categories[slug] = entry
        self._save()

    def mark_uploaded(self, slug: str, *, hdx_dataset: str | None) -> None:
        entry = self._state.categories.get(slug)
        if entry is None:
            return
        entry.uploaded_utc = _now_iso()
        entry.hdx_dataset = hdx_dataset
        self._save()

    def is_built(self, slug: str, *, snapshot_label: str) -> bool:
        entry = self.get(slug)
        if entry is None or entry.built_utc is None:
            return False
        if entry.snapshot_label != snapshot_label:
            return False
        return all(Path(p).exists() for p in entry.zip_paths)

    def is_uploaded(self, slug: str, *, snapshot_label: str) -> bool:
        entry = self.get(slug)
        if entry is None or entry.uploaded_utc is None:
            return False
        return entry.snapshot_label == snapshot_label

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": self._state.schema_version,
            "iso3": self._state.iso3,
            "source": self._state.source,
            "categories": {slug: asdict(entry) for slug, entry in self._state.categories.items()},
        }
        with tempfile.NamedTemporaryFile(
            "w",
            dir=self._path.parent,
            prefix=self._path.name + ".",
            suffix=".tmp",
            delete=False,
            encoding="utf-8",
        ) as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            tmp_path = Path(fh.name)
        os.replace(tmp_path, self._path)


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
