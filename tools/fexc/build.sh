#!/bin/sh
# Build the native macOS FEXCore compiler subset (static library, fexc CLI, smoke check).
#   build.sh ARCHIVE_DIR BUILD_DIR
# ARCHIVE_DIR holds the pinned upstream.tar.gz + dependency-overlay.tar (the I4-V
# producer's source/ directory). BUILD_DIR is created fresh: BUILD_DIR/src is the
# extracted+patched FEX tree (never committed), BUILD_DIR/obj the CMake/Ninja build.
set -eu
archives=$1
build=$2
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
pin_up=97df6a9ccd3b870b7616a294afc8e637aeab8cb1d85bb70fb4ca0538942b6079
pin_ov=17434ad9b7e62abaea61613ce35fda968e12b55c8d7314e9fba7a8c47d6cc701   # == tools/i4v_producer/producer.json source_inputs
[ "$(shasum -a 256 "$archives/upstream.tar.gz" | cut -d' ' -f1)" = "$pin_up" ] || { echo "upstream.tar.gz differs from pin" >&2; exit 1; }
[ "$(shasum -a 256 "$archives/dependency-overlay.tar" | cut -d' ' -f1)" = "$pin_ov" ] || { echo "overlay differs from pin" >&2; exit 1; }
mkdir "$build" "$build/src"
tar xzf "$archives/upstream.tar.gz" -C "$build/src" --strip-components=1   # FEX 48d71752e2a363d5535181f803b0b0e9666c1825
tar xf "$archives/dependency-overlay.tar" -C "$build/src"
python3 "$here/patch.py" "$build/src"
cmake -S "$here" -B "$build/obj" -G Ninja -DFEXC_SRC="$build/src" -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_C_COMPILER=clang -DCMAKE_CXX_COMPILER=clang++
cmake --build "$build/obj" --parallel 4
"$build/obj/fexc_linktest" --init
python3 "$here/smoke.py" "$build/obj/fexc" "$build/smoke"
