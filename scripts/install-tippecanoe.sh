#!/bin/sh
# Builds tippecanoe 2.82.0 and tippecanoe-decode into <prefix>/bin, prefix being the first argument
# (default /usr/local). No tagged release is 2.81 or later, so it is pinned to the 2.82.0 commit.
set -eu

commit=4f2621186acfec33b63ddf636f665623c0fef2dd
prefix="${1:-/usr/local}"
source_dir=$(mktemp -d)
trap 'rm -rf "$source_dir"' EXIT

curl --proto '=https' --tlsv1.2 -sSfL "https://github.com/felt/tippecanoe/archive/$commit.tar.gz" \
    | tar -xz -C "$source_dir" --strip-components=1
make -C "$source_dir" -j"$(nproc)" tippecanoe tippecanoe-decode
for tool in tippecanoe tippecanoe-decode; do
    install -D -m 0755 "$source_dir/$tool" "$prefix/bin/$tool"
done
