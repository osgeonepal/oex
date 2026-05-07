"""OSM PBF download with HTTP Range resume and optional md5 verification."""

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path

import requests
from tqdm import tqdm

from oex.logging_setup import get_logger

logger = get_logger(__name__)

_CHUNK = 1024 * 1024


@dataclass(frozen=True)
class DownloadResult:
    path: Path
    bytes_written: int
    md5_verified: bool
    snapshot_label: str


def download_pbf(
    url: str,
    dest_dir: str | os.PathLike[str],
    *,
    md5_url: str | None = None,
    filename: str | None = None,
) -> DownloadResult:
    dest_root = Path(dest_dir)
    dest_root.mkdir(parents=True, exist_ok=True)

    name = filename or url.rsplit("/", 1)[-1]
    target = dest_root / name

    head = requests.head(url, allow_redirects=True, timeout=60)
    head.raise_for_status()
    total = int(head.headers.get("Content-Length") or 0)
    last_modified = head.headers.get("Last-Modified", "unknown")

    start_byte = 0
    if target.exists():
        existing = target.stat().st_size
        if total and existing == total:
            logger.info("PBF already complete: %s (%d bytes)", target, existing)
            md5_ok = _verify_md5(target, md5_url) if md5_url else False
            return DownloadResult(target, existing, md5_ok, last_modified)
        if total and existing < total:
            logger.info("Resuming PBF download from byte %d / %d", existing, total)
            start_byte = existing

    headers = {"Range": f"bytes={start_byte}-"} if start_byte else {}
    mode = "ab" if start_byte else "wb"
    with requests.get(url, headers=headers, stream=True, timeout=300) as resp:
        resp.raise_for_status()
        with (
            target.open(mode) as fh,
            tqdm(
                total=total or None,
                initial=start_byte,
                unit="B",
                unit_scale=True,
                desc=name,
            ) as bar,
        ):
            for chunk in resp.iter_content(chunk_size=_CHUNK):
                if not chunk:
                    continue
                fh.write(chunk)
                bar.update(len(chunk))

    md5_ok = _verify_md5(target, md5_url) if md5_url else False
    return DownloadResult(target, target.stat().st_size, md5_ok, last_modified)


def _verify_md5(path: Path, md5_url: str) -> bool:
    logger.info("Fetching md5 sidecar: %s", md5_url)
    try:
        resp = requests.get(md5_url, timeout=60)
        resp.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("Could not fetch md5 sidecar (%s): %s", md5_url, exc)
        return False

    match = re.match(r"\s*([0-9a-fA-F]{32})", resp.text)
    if not match:
        logger.warning("Malformed md5 sidecar: %r", resp.text[:80])
        return False
    expected = match.group(1).lower()

    h = hashlib.md5()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(_CHUNK), b""):
            h.update(chunk)
    actual = h.hexdigest().lower()
    if actual != expected:
        raise RuntimeError(f"md5 mismatch for {path}: expected {expected}, got {actual}")
    logger.info("md5 verified for %s", path)
    return True
