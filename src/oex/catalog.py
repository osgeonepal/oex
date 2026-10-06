"""A root STAC catalog listing each area, and an area catalog listing its Collections."""

import json
import random
import time
from dataclasses import dataclass
from typing import Any, Protocol

import pystac

from oex.config.schema import RootConfig, StacCatalogConfig
from oex.logging_setup import get_logger
from oex.s3 import public_url
from oex.s3 import resolve as s3_resolve

logger = get_logger(__name__)

CATALOG_FILE = "catalog.json"
JSON = pystac.MediaType.JSON
_ATTEMPTS = 6


class CatalogConflictError(RuntimeError):
    """The catalog changed between reading and writing it."""


class CatalogStore(Protocol):
    def read(self, key: str) -> tuple[dict[str, Any] | None, str | None]: ...

    def write(self, key: str, document: dict[str, Any], version: str | None) -> None:
        """Writes only while the stored version is still `version`; None means absent."""
        ...


@dataclass(frozen=True)
class Place:
    key: str
    url: str


@dataclass(frozen=True)
class CatalogLayout:
    root: Place
    area: Place
    area_id: str
    # An optional catalog between the root and the area, such as one gathering activations.
    group: Place | None = None


def layout(cfg: RootConfig) -> CatalogLayout | None:
    s3 = cfg.output.s3
    if not cfg.output.stac.catalog or not s3.enabled:
        return None
    bucket, prefix, region, endpoint_url, _acl = s3_resolve(s3)
    if not bucket:
        raise ValueError("output.stac.catalog needs output.s3.bucket or OEX_S3_BUCKET")

    def place(*parts: str) -> Place:
        key = "/".join(part.strip("/") for part in (prefix, *parts, CATALOG_FILE) if part)
        return Place(
            key, public_url(bucket=bucket, key=key, region=region, endpoint_url=endpoint_url)
        )

    area = s3.folder or cfg.iso3.upper()
    group = cfg.output.stac.group
    return CatalogLayout(
        root=place(),
        area=place(area),
        area_id=f"{cfg.key}_{area}".strip("_").lower(),
        group=place(group) if group else None,
    )


def link_collections(
    where: CatalogLayout,
    stac: StacCatalogConfig,
    store: CatalogStore,
    collections: list[tuple[str, str]],
    *,
    area_title: str,
) -> None:
    root = where.root.url
    parent = where.group.url if where.group else root
    area = _catalog(
        where.area_id, area_title, f"Layers for {area_title}.", where.area.url, root, parent
    )
    _upsert(store, where.area.key, area, collections)
    top = (area_title, where.area.url)
    if where.group and stac.group:
        title = stac.group_title or stac.group.replace("_", " ").title()
        group = _catalog(
            stac.group, title, f"{title} under {stac.title}.", where.group.url, root, root
        )
        _upsert(store, where.group.key, group, [top])
        top = (title, where.group.url)
    _upsert(
        store, where.root.key, _catalog(stac.id, stac.title, stac.description, root, root), [top]
    )
    logger.info("catalog: %d Collection(s) linked under %s", len(collections), where.area.url)


def _catalog(
    catalog_id: str,
    title: str,
    description: str,
    self_url: str,
    root_url: str,
    parent_url: str | None = None,
) -> dict:
    links = [
        {"rel": "self", "href": self_url, "type": JSON},
        {"rel": "root", "href": root_url, "type": JSON},
    ]
    if parent_url:
        links.append({"rel": "parent", "href": parent_url, "type": JSON})
    return {
        "type": "Catalog",
        "stac_version": pystac.get_stac_version(),
        "id": catalog_id,
        "title": title,
        "description": description,
        "links": links,
    }


def _upsert(store: CatalogStore, key: str, fresh: dict, children: list[tuple[str, str]]) -> None:
    for attempt in range(_ATTEMPTS):
        current, version = store.read(key)
        merged = _with_children(current or fresh, children)
        if current is not None and merged == current:
            return
        try:
            store.write(key, merged, version)
            return
        except CatalogConflictError:
            time.sleep(min(2**attempt, 30) * random.uniform(0.5, 1.5))
    raise CatalogConflictError(f"{key}: still changing after {_ATTEMPTS} attempts")


def _with_children(document: dict, children: list[tuple[str, str]]) -> dict:
    links = [link for link in document["links"] if link["rel"] != "child"]
    by_href = {link["href"]: link for link in document["links"] if link["rel"] == "child"}
    for title, href in children:
        by_href[href] = {"rel": "child", "href": href, "type": JSON, "title": title}
    ordered = sorted(by_href.values(), key=lambda link: (link.get("title") or "", link["href"]))
    return {**document, "links": links + ordered}


class S3CatalogStore:
    """Conditional writes, so two runs updating one catalog cannot lose each other's link."""

    def __init__(self, cfg: RootConfig) -> None:
        import boto3

        self._bucket, _prefix, region, endpoint_url, self._acl = s3_resolve(cfg.output.s3)
        self._client = boto3.client("s3", region_name=region or None, endpoint_url=endpoint_url)

    def read(self, key: str) -> tuple[dict[str, Any] | None, str | None]:
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=key)
        except self._client.exceptions.NoSuchKey:
            return None, None
        return json.loads(response["Body"].read()), response["ETag"]

    def write(self, key: str, document: dict[str, Any], version: str | None) -> None:
        import botocore.exceptions

        condition = {"IfMatch": version} if version else {"IfNoneMatch": "*"}
        extra = {"ACL": self._acl} if self._acl else {}
        body = json.dumps(document, indent=2, ensure_ascii=False).encode("utf-8")
        try:
            self._client.put_object(
                Bucket=self._bucket, Key=key, Body=body, ContentType=JSON, **condition, **extra
            )
        except botocore.exceptions.ClientError as error:
            if error.response["Error"]["Code"] in (
                "PreconditionFailed",
                "ConditionalRequestConflict",
            ):
                raise CatalogConflictError(key) from error
            raise
