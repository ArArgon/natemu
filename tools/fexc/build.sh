#!/bin/sh
# Build the native macOS FEXCore compiler subset (static library, fexc CLI, smoke check).
#   build.sh BUILD_DIR
# Source: the FEX submodule third_party/FEX (pinned commit) plus the externals the build uses (fmt, xxhash,
# unordered_dense), patched with the series patches/*.patch in a scratch git worktree of the submodule. BUILD_DIR is
# created fresh: BUILD_DIR/src is that worktree (patched, a git checkout so the series can be regenerated with
# `git format-patch`; never committed), BUILD_DIR/obj the CMake/Ninja build. third_party/FEX itself is never modified.
set -eu
build=$1
case $build in /*) ;; *) build=$PWD/$build ;; esac   # git -C below would resolve a relative path inside the submodule
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
root=$(CDPATH= cd -- "$here/../.." && pwd)
fex=$root/third_party/FEX
pin=48d71752e2a363d5535181f803b0b0e9666c1825   # == the third_party/FEX gitlink (the old I4-V producer's upstream_commit)
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
git -C "$build/src" -c user.name=natemu -c user.email=natemu@example.invalid am -q --whitespace=nowarn "$here"/patches/*.patch
cmake -S "$here" -B "$build/obj" -G Ninja -DFEXC_SRC="$build/src" -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_C_COMPILER=clang -DCMAKE_CXX_COMPILER=clang++
cmake --build "$build/obj" --parallel 4
"$build/obj/fexc_linktest" --init
python3 "$here/smoke.py" "$build/obj/fexc" "$build/smoke"
