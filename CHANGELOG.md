# Changelog

All notable changes to this project will be documented in this file.

This project follows [Conventional Commits](https://www.conventionalcommits.org/)
and is auto-managed by [commitizen](https://commitizen-tools.github.io/commitizen/).

## 0.2.0 (2026-05-07)

### Feat

- **osm**: adds osm as support and refactors entire setup with modern python deps

### Fix

- **config**: adds config dataset inside CONFIG class
- **license**: fixes license url not being parsed issue

## 0.1.1 (2025-03-30)

### Fix

- **lcs**: adds layer creation option and fixes encoding issue

## 0.1.0 (2025-03-30)

### Feat

- **readme**: add default Python example for Overture2HDX configuration

### Fix

- **encode**: fixes bug on encoding and adds more attributes to datasets

## 0.0.6 (2025-03-30)

### Fix

- **file**: fixes bug on file too large
- **yaml**: fixes bug with categories in tphub

## 0.0.5 (2025-03-30)

### Fix

- **schema**: adds default schema and fixes issues with license
- **license**: adds license info in yaml

## 0.0.4 (2025-03-30)

### Fix

- **category**: adds category to hdx exports and fixes bug on shapefile generation

## 0.0.3 (2024-11-08)

### Fix

- **doc-generation**: fix for readme doc link to package

## 0.0.2 (2024-11-08)

### Fix

- **config**: config fix

## v0.4.0 (2026-06-02)

### Feat

- **clip**: added clip to planet boundary

## v0.3.0 (2026-05-17)

### Feat

- **h3**: add h3 index join instead of spatial to prevent memory issue and add performance ad
- **temporal**: add temporal info inthe report
- **language**: add local language support with babel
- **s3**: add s3 upload feature
- **transliterate**: add feature to transliterate the language
- **pcodes**: add pcode integration in datasets

### Fix

- **skip-null-pcode-rows-scan**: pcodes
- **fallback**: add fallabck to neighbour h3 grid rather than geos for large datasets
- **ci**: fixes test cases added h3 context
- **adaptive**: parallel resources to docker with osm cli
- **pcode**: add semaphore on pcode spatial joins
- **pcode**: fix the pcode join in big tables
- **path**: add preflight to fix writeable path
- **filesize**: fixes file size and add resume option
- **country**: parquet
- **loader**: planet
- **schema**: hot
- **s3**: uploader on schema
- **source**: fix source string in overture
- **overture**: fix on transliteratue
- **sources**: add report formatting with two sources
- **hdx**: fix hdx push bug
- **bug**: fixes precommit lock file bug

## v0.2.1 (2026-05-07)

### Fix

- **boundary**: fix boundary override on the configuration

## v0.2.0 (2026-05-07)

### Feat

- **osm**: adds osm as support and refactors entire setup with modern python deps
- **readme**: add default Python example for Overture2HDX configuration

### Fix

- **config**: adds config dataset inside CONFIG class
- **license**: fixes license url not being parsed issue
- **lcs**: adds layer creation option and fixes encoding issue
- **encode**: fixes bug on encoding and adds more attributes to datasets
- **file**: fixes bug on file too large
- **yaml**: fixes bug with categories in tphub
- **schema**: adds default schema and fixes issues with license
- **license**: adds license info in yaml
- **category**: adds category to hdx exports and fixes bug on shapefile generation
- **doc-generation**: fix for readme doc link to package
- **config**: config fix
