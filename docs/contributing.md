# Contributing

```bash
just setup         # uv sync + install hooks
just lint          # ruff + ty + file hygiene
just test          # unit tests (skips integration)
```

Conventional commits are enforced via commitizen on `commit-msg`.

## Releasing

```bash
uv run cz bump
git push --follow-tags
```

The `Release` workflow builds with `uv build` and publishes to PyPI via
`uv publish` using `PYPI_API_TOKEN` from the `pypi` GitHub environment.
