"""HDX dataset and resource publication. Imports hdx-python-api lazily."""

import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from oex.config.schema import CategoryConfig, HdxConfig, RootConfig
from oex.logging_setup import get_logger

logger = get_logger(__name__)


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

        logger.info("Creating HDX dataset %s", dt_name)
        dataset.create_in_hdx(allow_no_resources=True)

        for zip_path in zip_paths:
            fmt = zip_path.stem.rsplit("_", 1)[-1]
            resource = Resource(
                {
                    "name": zip_path.name,
                    "description": f"{category.name} data in {fmt.upper()} format",
                }
            )
            resource.set_format(fmt)
            resource.set_file_to_upload(str(zip_path))
            dataset.add_update_resource(resource)
        dataset.update_in_hdx()
        logger.info("Updated HDX dataset %s with %d resources", dt_name, len(zip_paths))
        return dt_name


def _slugify(value: str) -> str:
    import re

    return re.sub(r"[^a-zA-Z0-9]+", "_", value).lower().strip("_")
