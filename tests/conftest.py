"""Shared fixtures."""

import json
from pathlib import Path

import pytest

NEPAL_BBOX_GEOM = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "properties": {},
            "geometry": {
                "type": "Polygon",
                "coordinates": [
                    [
                        [83.91927014759125, 28.158212628626416],
                        [84.0342663278089, 28.158212628626416],
                        [84.0342663278089, 28.255549578267903],
                        [83.91927014759125, 28.255549578267903],
                        [83.91927014759125, 28.158212628626416],
                    ]
                ],
            },
        }
    ],
}


@pytest.fixture
def nepal_geom() -> str:
    return json.dumps(NEPAL_BBOX_GEOM)


@pytest.fixture
def nepal_config_yaml(tmp_path: Path, nepal_geom: str) -> Path:
    text = f"""
iso3: NPL
key: test_run
dataset_name: Pokhara Test
subnational: true
frequency: yearly
boundary:
  geom: |
    {nepal_geom}
output:
  dir: {tmp_path / "output"}
  formats:
    - gpkg
hdx:
  push: false
parallel:
  enabled: false
"""
    target = tmp_path / "nepal.yaml"
    target.write_text(text, encoding="utf-8")
    return target
