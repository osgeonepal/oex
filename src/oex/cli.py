"""Typer CLI for oex."""

from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Annotated

import typer

from oex.config.loader import (
    iter_configs,
    load_config,
    select_categories,
)
from oex.config.schema import RootConfig
from oex.exporter import Exporter, ExportResult
from oex.logging_setup import get_logger, setup_logging
from oex.osm.runner import OsmRunner
from oex.overture.runner import OvertureRunner
from oex.sources.file import FileRunner
from oex.sources.labels import RUN_SOURCES

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Country-scale OSM and Overture vector exports.",
)


Iso3OrYamlArgument = Annotated[
    str | None,
    typer.Argument(help="ISO3 like NPL, or name of a YAML in ./configs/ (prefer --iso3)"),
]
ThemeArgument = Annotated[
    str | None, typer.Argument(help="Optional theme override (e.g. buildings)")
]
ConfigsDirOption = Annotated[
    Path | None, typer.Option("--configs-dir", help="Run every YAML in this directory")
]
ConfigOption = Annotated[
    Path | None, typer.Option("--config", "-c", help="Explicit config YAML path")
]
Iso3Option = Annotated[
    str | None,
    typer.Option(
        "--iso3",
        help="ISO3 country code (e.g. NPL, COD). Overrides the positional argument and YAML.",
    ),
]
DatasetNameOption = Annotated[
    str | None,
    typer.Option(
        "--dataset-name",
        help=(
            "Free-form area label used as the {country} substitution in hdx.title_template. "
            "Set this to fix pycountry inversions (e.g. DRC) or for sub-national exports."
        ),
    ),
]
OutputDirOption = Annotated[Path | None, typer.Option("--output-dir", "-o")]
HdxPushOption = Annotated[bool | None, typer.Option("--hdx-push/--no-hdx-push")]
HdxPurgeOption = Annotated[
    bool | None,
    typer.Option(
        "--hdx-purge/--no-hdx-purge",
        help="Destructive: delete every existing resource on the dataset before upload.",
    ),
]
HdxCombineOption = Annotated[
    bool | None,
    typer.Option(
        "--hdx-combine/--no-hdx-combine",
        help="Publish every category onto one HDX dataset instead of one per category.",
    ),
]
PmtilesOption = Annotated[
    bool | None,
    typer.Option(
        "--pmtiles/--no-pmtiles",
        help="Generate PMTiles. With --hdx-combine, all layers merge into one tileset.",
    ),
]
S3Option = Annotated[
    bool | None,
    typer.Option("--s3/--no-s3", help="Upload artifacts to S3. Overrides output.s3.enabled."),
]
DownloadIfMissingOption = Annotated[
    bool | None,
    typer.Option(
        "--download-if-missing/--no-download-if-missing",
        help=(
            "When the planet path is missing, download the ~87 GB planet PBF "
            "before running. Overrides source.osm.auto_download_planet."
        ),
    ),
]
ResumeOption = Annotated[
    bool | None,
    typer.Option(
        "--resume/--no-resume",
        help=(
            "Skip categories already built and uploaded according to the "
            "state file. Default: enabled (configurable via output.resume)."
        ),
    ),
]


def _print_version(value: bool) -> None:
    if not value:
        return
    try:
        typer.echo(f"oex {version('oex')}")
    except PackageNotFoundError:
        typer.echo("oex (source checkout)")
    raise typer.Exit()


@app.callback()
def main_callback(
    log_level: str = typer.Option("INFO", envvar="LOG_LEVEL"),
    show_version: bool = typer.Option(
        False,
        "--version",
        "-V",
        callback=_print_version,
        is_eager=True,
        help="Show oex version and exit.",
    ),
) -> None:
    setup_logging(level=log_level)


def _resolve_config(
    iso3_or_yaml: str | None,
    configs_dir: Path | None,
    config: Path | None,
) -> list[Path | None]:
    if configs_dir is not None:
        return list(iter_configs(configs_dir))
    if config is not None:
        return [config]
    if iso3_or_yaml:
        candidate = Path("configs") / f"{iso3_or_yaml.lower()}.yaml"
        if candidate.exists():
            return [candidate]
    return [None]


def _build_overrides(
    iso3_or_yaml: str | None,
    hdx_push: bool | None,
    output_dir: Path | None,
    osm_engine: str | None = None,
    hdx_purge: bool | None = None,
    download_if_missing: bool | None = None,
    resume: bool | None = None,
    iso3: str | None = None,
    dataset_name: str | None = None,
    hdx_combine: bool | None = None,
    pmtiles: bool | None = None,
    s3: bool | None = None,
) -> dict[str, object]:
    overrides: dict[str, object] = {}
    if iso3_or_yaml and len(iso3_or_yaml) <= 3 and iso3_or_yaml.isalpha():
        overrides["iso3"] = iso3_or_yaml.upper()
    if iso3:
        overrides["iso3"] = iso3.upper()
    if dataset_name is not None:
        overrides["dataset_name"] = dataset_name
    toggles = {
        "hdx.push": hdx_push,
        "hdx.purge_existing_resources": hdx_purge,
        "hdx.combine": hdx_combine,
        "output.pmtiles.enabled": pmtiles,
        "output.s3.enabled": s3,
        "source.osm.auto_download_planet": download_if_missing,
        "output.resume": resume,
    }
    overrides.update({key: value for key, value in toggles.items() if value is not None})
    if output_dir is not None:
        overrides["output.dir"] = str(output_dir)
    if osm_engine is not None:
        overrides["source.osm.engine"] = osm_engine
    return overrides


def _summarise(results: list[ExportResult]) -> int:
    logger = get_logger("oex.cli")
    failed_count = sum(result.failed for result in results)
    for result in results:
        logger.info(
            "%s/%s: %d ok, %d unchanged, %d empty, %d skipped, %d failed in %.1fs",
            result.iso3,
            result.source_name,
            result.succeeded,
            result.unchanged,
            result.empty,
            result.skipped,
            result.failed,
            result.total_duration_s,
        )
    return 0 if failed_count == 0 else 1


_RUNNERS = {"osm": OsmRunner, "overture": OvertureRunner, "file": FileRunner}


def _load_with_overrides(yaml_path: Path | None, overrides: dict[str, object]) -> RootConfig:
    """CLI flags are merged before validation, so `--iso3` satisfies checks that need it."""
    return load_config(yaml_path, overrides)


def _run_one(
    yaml_path: Path | None,
    overrides: dict[str, object],
    theme: str | None,
    runner_factory,
) -> ExportResult:
    cfg = select_categories(_load_with_overrides(yaml_path, overrides), theme)
    return Exporter(cfg, runner_factory()).run()


def _enabled_sources(yaml_path: Path | None, overrides: dict[str, object]) -> list[str]:
    """Return the runnable source names in run order, honouring CLI overrides."""
    cfg = _load_with_overrides(yaml_path, overrides)
    return [name for name in RUN_SOURCES if name in cfg.source and cfg.source[name].enabled]


def _resolve_args(
    iso3_or_yaml: str | None,
    theme: str | None,
    configs_dir: Path | None,
    config: Path | None,
) -> tuple[str | None, str | None]:
    if configs_dir is None and config is None:
        return iso3_or_yaml, theme
    # Uppercase 3-letter alpha is the ISO3 convention; everything else is theme.
    if (
        iso3_or_yaml is not None
        and len(iso3_or_yaml) == 3
        and iso3_or_yaml.isalpha()
        and iso3_or_yaml.isupper()
    ):
        return iso3_or_yaml, theme
    return None, iso3_or_yaml if theme is None else theme


@app.command("overture")
def cmd_overture(
    iso3_or_yaml: Iso3OrYamlArgument = None,
    theme: ThemeArgument = None,
    configs_dir: ConfigsDirOption = None,
    config: ConfigOption = None,
    iso3: Iso3Option = None,
    dataset_name: DatasetNameOption = None,
    output_dir: OutputDirOption = None,
    hdx_push: HdxPushOption = None,
    hdx_purge: HdxPurgeOption = None,
    hdx_combine: HdxCombineOption = None,
    pmtiles: PmtilesOption = None,
    s3: S3Option = None,
    resume: ResumeOption = None,
) -> None:
    """Export Overture data."""
    iso3_resolved, theme_resolved = _resolve_args(iso3_or_yaml, theme, configs_dir, config)
    yaml_paths = _resolve_config(iso3 or iso3_resolved, configs_dir, config)
    overrides = _build_overrides(
        iso3_resolved,
        hdx_push,
        output_dir,
        hdx_purge=hdx_purge,
        iso3=iso3,
        dataset_name=dataset_name,
        hdx_combine=hdx_combine,
        pmtiles=pmtiles,
        s3=s3,
        resume=resume,
    )
    results = [
        _run_one(yaml_path, overrides, theme_resolved, OvertureRunner) for yaml_path in yaml_paths
    ]
    raise typer.Exit(code=_summarise(results))


@app.command("osm")
def cmd_osm(
    iso3_or_yaml: Iso3OrYamlArgument = None,
    theme: ThemeArgument = None,
    configs_dir: ConfigsDirOption = None,
    config: ConfigOption = None,
    iso3: Iso3Option = None,
    dataset_name: DatasetNameOption = None,
    output_dir: OutputDirOption = None,
    hdx_push: HdxPushOption = None,
    hdx_purge: HdxPurgeOption = None,
    engine: str | None = typer.Option(
        None,
        "--engine",
        help=("OSM engine: 'geofabrik' (default), 'planet', or 'postpass'/'rawdata' for live OSM."),
    ),
    download_if_missing: DownloadIfMissingOption = None,
    resume: ResumeOption = None,
    hdx_combine: HdxCombineOption = None,
    pmtiles: PmtilesOption = None,
    s3: S3Option = None,
) -> None:
    """Export OSM data via the configured engine."""
    iso3_resolved, theme_resolved = _resolve_args(iso3_or_yaml, theme, configs_dir, config)
    yaml_paths = _resolve_config(iso3 or iso3_resolved, configs_dir, config)
    overrides = _build_overrides(
        iso3_resolved,
        hdx_push,
        output_dir,
        osm_engine=engine,
        hdx_purge=hdx_purge,
        download_if_missing=download_if_missing,
        resume=resume,
        iso3=iso3,
        dataset_name=dataset_name,
        hdx_combine=hdx_combine,
        pmtiles=pmtiles,
        s3=s3,
    )
    results = [
        _run_one(yaml_path, overrides, theme_resolved, OsmRunner) for yaml_path in yaml_paths
    ]
    raise typer.Exit(code=_summarise(results))


@app.command("file")
def cmd_file(
    configs_dir: ConfigsDirOption = None,
    config: ConfigOption = None,
    iso3: str | None = typer.Option(
        None, "--iso3", help="ISO3 country code; required when pcode tagging is enabled."
    ),
    dataset_name: DatasetNameOption = None,
    output_dir: OutputDirOption = None,
    hdx_push: HdxPushOption = None,
    hdx_combine: HdxCombineOption = None,
    pmtiles: PmtilesOption = None,
    s3: S3Option = None,
) -> None:
    """Export categories read from spatial data files.

    Each category names a file and maps its columns, and the export then follows the same
    path as any other source: clipped to the boundary, pcode tagged, written to every
    configured format, and published with the metadata the config carries.
    """
    yaml_paths = _resolve_config(iso3, configs_dir, config)
    overrides = _build_overrides(
        None,
        hdx_push,
        output_dir,
        iso3=iso3,
        dataset_name=dataset_name,
        hdx_combine=hdx_combine,
        pmtiles=pmtiles,
        s3=s3,
    )
    results = [_run_one(yaml_path, overrides, None, FileRunner) for yaml_path in yaml_paths]
    raise typer.Exit(code=_summarise(results))


@app.command("all")
def cmd_all(
    iso3_or_yaml: Iso3OrYamlArgument = None,
    theme: ThemeArgument = None,
    configs_dir: ConfigsDirOption = None,
    config: ConfigOption = None,
    iso3: Iso3Option = None,
    dataset_name: DatasetNameOption = None,
    output_dir: OutputDirOption = None,
    hdx_push: HdxPushOption = None,
    hdx_purge: HdxPurgeOption = None,
    engine: str | None = typer.Option(
        None,
        "--engine",
        help="OSM engine override for the OSM half of the run. Ignored by the Overture run.",
    ),
    download_if_missing: DownloadIfMissingOption = None,
    resume: ResumeOption = None,
    hdx_combine: HdxCombineOption = None,
    pmtiles: PmtilesOption = None,
    s3: S3Option = None,
) -> None:
    """Export every source the config enables, in order (OSM then Overture).

    Each enabled source runs into the same config, so with ``hdx.combine`` the
    second source accumulates onto the dataset the first published.
    """
    iso3_resolved, theme_resolved = _resolve_args(iso3_or_yaml, theme, configs_dir, config)
    yaml_paths = _resolve_config(iso3 or iso3_resolved, configs_dir, config)
    overrides = _build_overrides(
        iso3_resolved,
        hdx_push,
        output_dir,
        osm_engine=engine,
        hdx_purge=hdx_purge,
        download_if_missing=download_if_missing,
        resume=resume,
        iso3=iso3,
        dataset_name=dataset_name,
        hdx_combine=hdx_combine,
        pmtiles=pmtiles,
        s3=s3,
    )
    results: list[ExportResult] = []
    for yaml_path in yaml_paths:
        sources = _enabled_sources(yaml_path, overrides)
        if not sources:
            raise typer.BadParameter(
                "no sources enabled; set source.osm.enabled or source.overture.enabled"
            )
        for name in sources:
            results.append(_run_one(yaml_path, overrides, theme_resolved, _RUNNERS[name]))
    raise typer.Exit(code=_summarise(results))


@app.command("metadata")
def cmd_metadata(
    configs_dir: ConfigsDirOption = None,
    config: ConfigOption = None,
    iso3: str | None = typer.Option(None, "--iso3", help="ISO3 country code (e.g. NPL, COD)"),
    check: bool = typer.Option(
        False, "--check", help="Report what would change and write nothing."
    ),
    prune: bool = typer.Option(
        False,
        "--prune",
        help="Destructive: delete resources whose category is no longer in the config.",
    ),
) -> None:
    """Push config text to HDX without re-running the export.

    Rewrites the dataset title, description, caveats, source and tags, and every
    resource's name and description, from the config. Data files are untouched, so
    a wording change takes seconds instead of a full export.
    """
    from oex.hdx_publisher import HdxPublisher

    yaml_paths = _resolve_config(iso3, configs_dir, config)
    overrides = _build_overrides(None, None, None, iso3=iso3)
    failed = 0
    for yaml_path in yaml_paths:
        cfg = _load_with_overrides(yaml_path, overrides)
        if not cfg.hdx.combined.name:
            typer.echo(f"{yaml_path}: no hdx.combined.name, skipping")
            continue
        publisher = HdxPublisher(cfg.hdx)
        try:
            hdx_dataset_name, changed, pruned = publisher.update_metadata(
                cfg, dry_run=check, prune=prune
            )
        except RuntimeError as error:
            typer.echo(f"{yaml_path}: {error}")
            failed += 1
            continue
        verb = "would change" if check else "changed"
        removed = f", {verb} {pruned} removed" if pruned else ""
        typer.echo(f"{hdx_dataset_name}: {verb} {changed} resource(s){removed}")
    raise typer.Exit(code=1 if failed else 0)


@app.command("osm-build-cache")
def cmd_osm_build_cache(
    config: Path | None = typer.Option(
        None, "--config", "-c", help="Config providing source.osm settings"
    ),
) -> None:
    """Download the planet OSM PBF to ``<cache_dir>/_pbf/planet-latest.osm.pbf``.

    Per-country extraction from that PBF happens lazily in ``oex-cli osm <ISO3>``
    when ``source.osm.engine`` is ``planet`` (or ``geofabrik`` with
    ``planet_fallback: true``).
    """
    from oex.osm.fetch_planet import download_pbf

    cfg: RootConfig = load_config(config)
    osm_config = cfg.source["osm"]
    pbf_dir = Path(osm_config.cache_dir) / "_pbf"
    result = download_pbf(osm_config.pbf_url, pbf_dir, md5_url=osm_config.md5_url)
    typer.echo(f"Planet PBF available at: {result.path}")
    typer.echo(
        "Set source.osm.pbf_path to that file to use the planet engine, "
        "or run `oex-cli osm <ISO3>` if your config already points to it."
    )


if __name__ == "__main__":
    app()
