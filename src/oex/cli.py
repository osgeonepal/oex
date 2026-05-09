"""Typer CLI for oex."""

from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import typer

from oex.config.loader import (
    apply_overrides,
    iter_configs,
    load_config,
    select_categories,
)
from oex.config.schema import RootConfig
from oex.exporter import Exporter, ExportResult
from oex.logging_setup import get_logger, setup_logging
from oex.osm.runner import OsmRunner
from oex.overture.runner import OvertureRunner

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Country-scale OSM and Overture vector exports.",
)


def _print_version(value: bool) -> None:
    if not value:
        return
    try:
        typer.echo(f"oex {version('oex')}")
    except PackageNotFoundError:
        typer.echo("oex (source checkout)")
    raise typer.Exit()


@app.callback()
def _global(
    log_level: str = typer.Option("INFO", envvar="LOG_LEVEL"),
    _v: bool = typer.Option(
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
) -> dict[str, object]:
    overrides: dict[str, object] = {}
    if iso3_or_yaml and len(iso3_or_yaml) <= 3 and iso3_or_yaml.isalpha():
        overrides["iso3"] = iso3_or_yaml.upper()
    if hdx_push is True:
        overrides["hdx.push"] = True
    if hdx_push is False:
        overrides["hdx.push"] = False
    if hdx_purge is True:
        overrides["hdx.purge_existing_resources"] = True
    if hdx_purge is False:
        overrides["hdx.purge_existing_resources"] = False
    if output_dir is not None:
        overrides["output.dir"] = str(output_dir)
    if osm_engine is not None:
        overrides["source.osm.engine"] = osm_engine
    return overrides


def _summarise(results: list[ExportResult]) -> int:
    log = get_logger("oex.cli")
    total_fail = sum(r.failed for r in results)
    for r in results:
        log.info(
            "%s/%s: %d ok, %d empty, %d skipped, %d failed in %.1fs",
            r.iso3,
            r.source_name,
            r.succeeded,
            r.empty,
            r.skipped,
            r.failed,
            r.total_duration_s,
        )
    return 0 if total_fail == 0 else 1


def _run_one(
    yaml_path: Path | None,
    overrides: dict[str, object],
    theme: str | None,
    runner_factory,
) -> ExportResult:
    cfg: RootConfig = load_config(yaml_path)
    cfg = apply_overrides(cfg, overrides)
    cfg = select_categories(cfg, theme)
    return Exporter(cfg, runner_factory()).run()


def _resolve_args(
    arg1: str | None,
    arg2: str | None,
    configs_dir: Path | None,
    config: Path | None,
) -> tuple[str | None, str | None]:
    if configs_dir is None and config is None:
        return arg1, arg2
    # Uppercase 3-letter alpha is the ISO3 convention; everything else is theme.
    if arg1 is not None and len(arg1) == 3 and arg1.isalpha() and arg1.isupper():
        return arg1, arg2
    return None, arg1 if arg2 is None else arg2


@app.command("overture")
def cmd_overture(
    iso3_or_yaml: str | None = typer.Argument(
        None, help="ISO3 like NPL, or name of a YAML in ./configs/"
    ),
    theme: str | None = typer.Argument(None, help="Optional theme override (e.g. buildings)"),
    configs_dir: Path | None = typer.Option(
        None, "--configs-dir", help="Run every YAML in this directory"
    ),
    config: Path | None = typer.Option(None, "--config", "-c", help="Explicit config YAML path"),
    output_dir: Path | None = typer.Option(None, "--output-dir", "-o"),
    hdx_push: bool | None = typer.Option(None, "--hdx-push/--no-hdx-push"),
    hdx_purge: bool | None = typer.Option(
        None,
        "--hdx-purge/--no-hdx-purge",
        help="Destructive: delete every existing resource on the dataset before upload.",
    ),
) -> None:
    """Export Overture data."""
    iso3_resolved, theme_resolved = _resolve_args(iso3_or_yaml, theme, configs_dir, config)
    yamls = _resolve_config(iso3_resolved, configs_dir, config)
    overrides = _build_overrides(iso3_resolved, hdx_push, output_dir, hdx_purge=hdx_purge)
    results = [_run_one(y, overrides, theme_resolved, OvertureRunner) for y in yamls]
    raise typer.Exit(code=_summarise(results))


@app.command("osm")
def cmd_osm(
    iso3_or_yaml: str | None = typer.Argument(
        None, help="ISO3 like NPL, or name of a YAML in ./configs/"
    ),
    theme: str | None = typer.Argument(None, help="Optional theme override (e.g. buildings)"),
    configs_dir: Path | None = typer.Option(None, "--configs-dir"),
    config: Path | None = typer.Option(None, "--config", "-c"),
    output_dir: Path | None = typer.Option(None, "--output-dir", "-o"),
    hdx_push: bool | None = typer.Option(None, "--hdx-push/--no-hdx-push"),
    hdx_purge: bool | None = typer.Option(
        None,
        "--hdx-purge/--no-hdx-purge",
        help="Destructive: delete every existing resource on the dataset before upload.",
    ),
    engine: str | None = typer.Option(
        None,
        "--engine",
        help="OSM engine: 'geofabrik' (default) or 'planet_parquet'",
    ),
) -> None:
    """Export OSM data via the configured engine."""
    iso3_resolved, theme_resolved = _resolve_args(iso3_or_yaml, theme, configs_dir, config)
    yamls = _resolve_config(iso3_resolved, configs_dir, config)
    overrides = _build_overrides(
        iso3_resolved, hdx_push, output_dir, osm_engine=engine, hdx_purge=hdx_purge
    )
    results = [_run_one(y, overrides, theme_resolved, OsmRunner) for y in yamls]
    raise typer.Exit(code=_summarise(results))


@app.command("osm-build-cache")
def cmd_osm_build_cache(
    pbf: Path | None = typer.Option(None, "--pbf", help="Local PBF path"),
    planet: bool = typer.Option(False, "--planet", help="Download the latest planet PBF"),
    config: Path | None = typer.Option(
        None, "--config", "-c", help="Config to drive theme tag filters"
    ),
    snapshot: str | None = typer.Option(
        None, "--snapshot", help="Snapshot label, defaults to today"
    ),
    themes: str | None = typer.Option(
        None, "--themes", help="Comma-separated theme slugs to limit"
    ),
) -> None:
    """Build the planet OSM PBF -> per-theme parquet cache at <cache_dir>/planet/<snapshot>/."""
    from oex.osm.build_cache import build_cache
    from oex.osm.fetch_planet import download_pbf

    cfg: RootConfig = load_config(config)

    if planet and pbf is not None:
        raise typer.BadParameter("Pass either --planet or --pbf, not both")
    if not planet and pbf is None:
        raise typer.BadParameter("One of --planet or --pbf is required")

    if planet:
        src = cfg.source["osm"]
        result = download_pbf(src.pbf_url, src.cache_dir + "/_pbf", md5_url=src.md5_url)
        pbf_path = result.path
    else:
        assert pbf is not None
        pbf_path = pbf

    theme_list = [t.strip() for t in themes.split(",")] if themes else None
    cache_root = Path(cfg.source["osm"].cache_dir) / "planet"
    manifest = build_cache(
        cfg,
        pbf_path,
        cache_root=cache_root,
        snapshot=snapshot,
        themes_filter=theme_list,
    )
    typer.echo(f"Cache snapshot: {manifest.snapshot}")
    typer.echo(f"Themes built: {[t.theme for t in manifest.themes]}")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
