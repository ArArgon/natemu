#!/bin/sh
# Build the INV-01-14 resource-budget capability probe. Apple clang only, no
# third-party dependency.
set -eu
cd "$(dirname "$0")"
build_dir=${1:-build}
mkdir -p "$build_dir"
clang -Wall -Wextra -Werror -O1 -o "$build_dir/probe" src/probe.c
codesign -s - -f "$build_dir/probe" >/dev/null 2>&1
echo "built: $build_dir/probe (ad-hoc signed, no entitlement)"
