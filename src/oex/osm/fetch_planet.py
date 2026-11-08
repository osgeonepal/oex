"""OSM PBF download with HTTP Range resume and optional md5 verification."""

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path

import requests
from tqdm import tqdm

from oex import http
from oex.logging_setup import get_logger

logger = get_logger(__name__)

_CHUNK_BYTES = 1024 * 1024


class Md5UnavailableError(RuntimeError):
    """The md5 sidecar could not be read, so the download is kept but not trusted; the next
    run checks it again without downloading."""


class Md5MismatchError(RuntimeError):
    """The download differs from its md5, such as a resume joining two versions of a file."""


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
    # Written under another name until verified, so an existing target is always complete.
    partial = dest_root / f"{name}.part"

    head = http.session.head(url, allow_redirects=True, timeout=60)
    head.raise_for_status()
    total = int(head.headers.get("Content-Length") or 0)
    last_modified = head.headers.get("Last-Modified", "unknown")

    if target.exists() and total and target.stat().st_size == total:
        logger.info("PBF already complete: %s (%d bytes)", target, total)
        md5_verified = _verify_md5(target, md5_url) if md5_url else False
        return DownloadResult(target, total, md5_verified, last_modified)

    partial_size = partial.stat().st_size if partial.exists() else 0
    if total and partial_size == total:
        logger.info("PBF download already complete, checking it: %s", partial)
    else:
        _download_to(partial, url, total, start_byte=partial_size if partial_size < total else 0)

    try:
        md5_verified = _verify_md5(partial, md5_url) if md5_url else False
    except Md5MismatchError:
        partial.unlink()
        raise
    partial.replace(target)
    return DownloadResult(target, target.stat().st_size, md5_verified, last_modified)


def _download_to(partial: Path, url: str, total: int, *, start_byte: int) -> None:
    if start_byte:
        logger.info("Resuming PBF download from byte %d / %d", start_byte, total)
    headers = {"Range": f"bytes={start_byte}-"} if start_byte else {}
    mode = "ab" if start_byte else "wb"
    with requests.get(url, headers=headers, stream=True, timeout=300) as response:
        response.raise_for_status()
        with (
            partial.open(mode) as partial_file,
            tqdm(
                total=total or None,
                initial=start_byte,
                unit="B",
                unit_scale=True,
                desc=partial.name,
            ) as progress,
        ):
            for chunk in response.iter_content(chunk_size=_CHUNK_BYTES):
                if not chunk:
                    continue
                partial_file.write(chunk)
                progress.update(len(chunk))


def _verify_md5(path: Path, md5_url: str) -> bool:
    logger.info("Fetching md5 sidecar: %s", md5_url)
    try:
        response = http.session.get(md5_url, timeout=60)
        response.raise_for_status()
    except requests.RequestException as error:
        raise Md5UnavailableError(f"Could not fetch md5 sidecar {md5_url}: {error}") from error

    match = re.match(r"\s*([0-9a-fA-F]{32})", response.text)
    if not match:
        raise Md5UnavailableError(f"Malformed md5 sidecar at {md5_url}: {response.text[:80]!r}")
    expected = match.group(1).lower()

    digest = hashlib.md5()
    with path.open("rb") as pbf_file:
        for chunk in iter(lambda: pbf_file.read(_CHUNK_BYTES), b""):
            digest.update(chunk)
    actual = digest.hexdigest().lower()
    if actual != expected:
        raise Md5MismatchError(f"md5 mismatch for {path}: expected {expected}, got {actual}")
    logger.info("md5 verified for %s", path)
    return True
