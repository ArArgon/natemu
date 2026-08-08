#!/bin/sh
# Build + ad-hoc sign a probe with the hypervisor entitlement.
# usage: ./build.sh <dir>/<name>.c   -> <dir>/bin/<name>
set -e
src="$1"; dir=$(dirname "$src"); name=$(basename "$src" .c)
mkdir -p "$dir/bin"
clang -O2 -g -arch arm64 -framework Hypervisor -o "$dir/bin/$name" "$src"
codesign --sign - --force --entitlements "$(dirname "$0")/entitlements.plist" "$dir/bin/$name"
echo "built $dir/bin/$name"
