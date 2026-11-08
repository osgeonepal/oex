# Contributing

```bash
just setup
just lint
just test
just run osm nepal
```

`just setup` syncs the environment and installs the pre-commit hooks. `just lint` runs
the hooks: ruff check, ruff format, the uv lock check, ty and file hygiene. `just test` runs the test suite
offline. Tests marked `network` fetch STAC schemas over HTTPS and are deselected by
default; `just test -m network` runs them. `just run` passes its arguments to `oex-cli`.

Conventional commits are enforced via commitizen on `commit-msg`.

## Releasing

```bash
uv run cz bump
git push --follow-tags
```

The `Release` workflow builds with `uv build` and publishes to PyPI via
`uv publish` using `PYPI_API_TOKEN` from the `pypi` GitHub environment.
