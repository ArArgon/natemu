#!/bin/sh
# Build the native macOS FEXCore compiler subset (static library, fexc CLI, smoke check).
#   build.sh BUILD_DIR
# Source: the FEX submodule third_party/FEX, pinned to a commit of the fork https://github.com/ArArgon/natemu-fex (branch
# `natemu`: upstream 48d71752 plus the patch series, one commit per patch) plus the externals the build uses (fmt, xxhash,
# unordered_dense). BUILD_DIR is created fresh: BUILD_DIR/src is a scratch git worktree of the submodule at the pin (never
# committed), BUILD_DIR/obj the CMake/Ninja build. third_party/FEX itself is never modified.
set -eu
build=$1
case $build in /*) ;; *) build=$PWD/$build ;; esac   # git -C below would resolve a relative path inside the submodule
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
root=$(CDPATH= cd -- "$here/../.." && pwd)
fex=$root/third_party/FEX
pin=8d812cadd94f4be5c62bad609b53a7515c7c077f   # == the third_party/FEX gitlink: the fork's `natemu` branch (PCID-phase P2)
externals="fmt xxhash unordered_dense"
git -C "$root" submodule update --init --depth 1 third_party/FEX
for e in $externals; do git -C "$fex" submodule update --init --depth 1 External/$e; done
[ "$(git -C "$fex" rev-parse HEAD)" = "$pin" ] || { echo "third_party/FEX is not at the pinned commit $pin" >&2; exit 1; }
git -C "$fex" worktree prune
mkdir "$build"
git -C "$fex" worktree add -q --detach "$build/src" "$pin"
for e in $externals; do
  mkdir -p "$build/src/External/$e"
  git -C "$fex/External/$e" archive HEAD | tar x -C "$build/src/External/$e"
done
cmake -S "$here" -B "$build/obj" -G Ninja -DFEXC_SRC="$build/src" -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_C_COMPILER=clang -DCMAKE_CXX_COMPILER=clang++
cmake --build "$build/obj" --parallel 4
"$build/obj/fexc_linktest" --init
python3 "$here/smoke.py" "$build/obj/fexc" "$build/smoke"
