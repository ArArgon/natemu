#!/bin/sh
# Build the INV-01-01 capability probe. Apple clang only, no dependency.
set -eu
cd "$(dirname "$0")"
mkdir -p build
clang -Wall -Wextra -O1 -framework Hypervisor -o build/probe src/probe.c
codesign -s - -f build/probe >/dev/null 2>&1
echo "built: build/probe (ad-hoc signed, no entitlement)"
