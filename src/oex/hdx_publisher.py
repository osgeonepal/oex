"""HDX dataset and resource publication. Imports hdx-python-api lazily."""

import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from oex.config.schema import CategoryConfig, HdxConfig, RootConfig
from oex.logging_setup import get_logger

logger = get_logger(__name__)

_HDX_SITE_URLS = {
    "prod": "https://data.humdata.org",
    "demo": "https://demo.data-humdata-org.ahconu.org",
    "stage": "https://stage.data-humdata-org.ahconu.org",
}


@dataclass
class PublishContext:
    dataset_source: str
    snapshot_date: datetime


class HdxPublisher:
    def __init__(self, hdx_cfg: HdxConfig):
        if not hdx_cfg.push:
            raise ValueError("HdxPublisher constructed but hdx.push is false")
        api_key = hdx_cfg.api_key or os.environ.get("HDX_API_KEY")
        owner_org = hdx_cfg.owner_org or os.environ.get("HDX_OWNER_ORG")
        maintainer = hdx_cfg.maintainer or os.environ.get("HDX_MAINTAINER")
        if not (api_key and owner_org and maintainer):
            raise ValueError(
                "hdx.push is enabled but HDX credentials (api_key, owner_org, "
                "maintainer) are missing. Set them in the YAML or environment."
            )

        _preflight_check(api_key, owner_org, maintainer, hdx_cfg.site)

        from hdx.api.configuration import Configuration

        Configuration.create(
            hdx_site=hdx_cfg.site,
            hdx_key=api_key,
            user_agent=hdx_cfg.user_agent,
        )
        self._owner_org = owner_org
        self._maintainer = maintainer
        logger.info("HDX session ready: site=%s org=%s", hdx_cfg.site, owner_org)

    def publish(
        self,
        cfg: RootConfig,
        category: CategoryConfig,
        zip_paths: list[Path],
        ctx: PublishContext,
    ) -> str:
        from hdx.data.dataset import Dataset
        from hdx.data.resource import Resource

        category_slug = _slugify(category.name)
        dt_name = f"{cfg.key}_{cfg.iso3.lower()}_{category_slug}"
        title = category.hdx.title or f"{category.name} of {cfg.dataset_name or cfg.iso3.upper()}"

        dataset_args: dict[str, object] = {
            "title": title,
            "name": dt_name,
            "notes": category.hdx.notes,
            "caveats": category.hdx.caveats,
            "private": False,
            "dataset_source": category.hdx.dataset_source or ctx.dataset_source,
            "methodology": cfg.hdx.methodology,
            "methodology_other": cfg.hdx.methodology_other,
            "owner_org": self._owner_org,
            "maintainer": self._maintainer,
            "subnational": cfg.subnational,
        }
        if category.hdx.license == "hdx-odc-odbl":
            dataset_args["license_id"] = "hdx-odc-odbl"
        else:
            dataset_args["license_id"] = "hdx-other"
            dataset_args["license_other"] = category.hdx.license
        if category.hdx.license_url:
            dataset_args["license_url"] = category.hdx.license_url

        dataset = Dataset(dataset_args)
        dataset.set_time_period(ctx.snapshot_date)
        dataset.set_expected_update_frequency(cfg.frequency)
        dataset.add_other_location(cfg.iso3.upper())
        for tag in category.hdx.tags:
            dataset.add_tag(tag)

        logger.info("Creating/updating HDX dataset %s (metadata)", dt_name)
        dataset.create_in_hdx(allow_no_resources=True)

        fresh = Dataset.read_from_hdx(dt_name)
        if fresh is None:
            raise RuntimeError(f"HDX dataset {dt_name} not visible after create")
        package_id = fresh["id"]
        existing_by_name: dict[str, Resource] = {
            r["name"]: r for r in (fresh.get_resources() or [])
        }

        ok = 0
        failed: list[tuple[str, str]] = []
        for zip_path in zip_paths:
            size_mb = zip_path.stat().st_size / (1024 * 1024)
            parts = zip_path.stem.rsplit("_", 2)
            source = parts[-2] if len(parts) >= 3 else ""
            fmt = parts[-1]
            description = (
                f"{category.name} ({source}) data in {fmt.upper()} format"
                if source
                else f"{category.name} data in {fmt.upper()} format"
            )
            try:
                if zip_path.name in existing_by_name:
                    res = existing_by_name[zip_path.name]
                    res["description"] = description
                    res.set_format(fmt)
                    res.set_file_to_upload(str(zip_path))
                    logger.info("Updating resource %s (%.0f MB)", zip_path.name, size_mb)
                    res.update_in_hdx()
                else:
                    res = Resource(
                        {
                            "package_id": package_id,
                            "name": zip_path.name,
                            "description": description,
                        }
                    )
                    res.set_format(fmt)
                    res.set_file_to_upload(str(zip_path))
                    logger.info("Creating resource %s (%.0f MB)", zip_path.name, size_mb)
                    res.create_in_hdx()
                ok += 1
            except Exception as exc:  # noqa: BLE001  per-resource boundary; reported below
                logger.exception("Resource upload failed for %s (%.0f MB)", zip_path.name, size_mb)
                failed.append((zip_path.name, str(exc)))

        if failed:
            summary = "; ".join(f"{name}: {err}" for name, err in failed)
            raise RuntimeError(
                f"HDX dataset {dt_name}: {ok}/{len(zip_paths)} resources uploaded; "
                f"failures: {summary}"
            )
        logger.info("Saved HDX dataset %s with %d resources", dt_name, len(zip_paths))
        return dt_name


def _slugify(value: str) -> str:
    import re

    return re.sub(r"[^a-zA-Z0-9]+", "_", value).lower().strip("_")


def _preflight_check(api_key: str, owner_org: str, maintainer: str, site: str) -> None:
    """Fail fast with a precise message if HDX credentials cannot publish to owner_org.
    This runs three cheap CKAN action calls before any export work begins.
    """
    import requests

    base = _HDX_SITE_URLS.get(site)
    if base is None:
        raise ValueError(f"Unknown hdx.site={site!r}. Expected one of {sorted(_HDX_SITE_URLS)}.")
    headers = {"Authorization": api_key, "User-Agent": "oex-preflight"}

    def _post(action: str, payload: dict) -> dict:
        url = f"{base}/api/3/action/{action}"
        try:
            r = requests.post(url, json=payload, headers=headers, timeout=15)
        except requests.RequestException as exc:
            raise ValueError(f"HDX preflight: network error reaching {url}: {exc}") from exc
        try:
            body = r.json()
        except ValueError as exc:
            raise ValueError(
                f"HDX preflight: non-JSON response from {url} (status {r.status_code}): "
                f"{r.text[:200]}"
            ) from exc
        return body

    orgs_body = _post("organization_list_for_user", {"permission": "update_dataset"})
    if not orgs_body.get("success"):
        err = orgs_body.get("error", {})
        raise ValueError(
            f"HDX preflight failed at {base}: API key did not authenticate. "
            f"CKAN says: {err}. Check that HDX_API_KEY is set in this shell "
            f"(echo $HDX_API_KEY) and is valid at {base}/user/<you>/api-tokens."
        )
    orgs = orgs_body.get("result") or []
    org_names = {o.get("name") for o in orgs}
    org_ids = {o.get("id") for o in orgs}
    if owner_org not in org_names and owner_org not in org_ids:
        editable = sorted(n for n in org_names if n)
        raise ValueError(
            f"HDX preflight failed at {base}: token user has no edit rights on "
            f"{owner_org!r}. Orgs with update_dataset permission for this token: "
            f"{editable or '(none)'}. Ask an org admin to add you as Editor at "
            f"{base}/organization/{owner_org}/about, or use a token from a member "
            f"account."
        )

    user_body = _post("user_show", {"id": maintainer})
    if not user_body.get("success"):
        err = user_body.get("error", {})
        raise ValueError(
            f"HDX preflight failed at {base}: maintainer {maintainer!r} not found. "
            f"CKAN says: {err}. Set HDX_MAINTAINER (or hdx.maintainer in YAML) to "
            f"your HDX profile slug from {base}/dashboard."
        )

    fullname = user_body.get("result", {}).get("fullname") or maintainer
    logger.info(
        "HDX preflight ok: token can edit %s as %s (%s)",
        owner_org,
        fullname,
        maintainer,
    )
