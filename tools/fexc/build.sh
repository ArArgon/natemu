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
pin=daa67e4a003bf8e8906fa3274ce3d62fd8188bc1   # == third_party/FEX gitlink: 6d2f5d8 + the SIMD DE screen's hoist hooks (FexcHoistPlan/FexcHoistEmit; the shim does the rest); 6d2f5d8 is 275de77 + FexcFmaCheck (AFP accurate chunk 4); 275de77 is 93350e2 + the NaN-sign-keeping negations; 93350e2 (720fe1c (807033d (791857f (726f3c0 (8254266 (7e8f5ed (13e80cf (1df70f0 + non-AFP MINSS/MAXSS signalling compare) + FexcFlush) + FexcSimdCheckDe/FexcX87CheckDe (AFP fallback 3b)) + non-AFP scalar direct write (MarkUpperZero)) + FexcKeep (unmasked UE tininess) + the FexcX87NanOp declaration (x87 NaN fix-up)) + FexcSimdDe (accurate masked SSE/AVX DE)) + FexcSimdSkip/FexcSimdSkipEnd (its screen)) + FexcSimdSkip without a result register) + NaN-sign-keeping negations without AFP (cached multipliers for the packed FMA addend negations, ADDSUB as fmul and fadd)
externals="fmt xxhash unordered_dense"
# A symlinked third_party/FEX (a git worktree sharing the main checkout's initialised submodule) is used as it is: git refuses to
# update a submodule path that is a symlink. The pin check below still applies.
if [ ! -L "$root/third_party/FEX" ]; then
  git -C "$root" submodule update --init --depth 1 third_party/FEX
  for e in $externals; do git -C "$fex" submodule update --init --depth 1 External/$e; done
fi
# A shared checkout may sit on another branch: the pin only has to exist in it (the build is a detached worktree at the pin).
{ [ -L "$root/third_party/FEX" ] || [ "$(git -C "$fex" rev-parse HEAD)" = "$pin" ]; } && git -C "$fex" cat-file -e "$pin^{commit}" ||
  { echo "third_party/FEX is not at the pinned commit $pin" >&2; exit 1; }
# The pin is the superproject's gitlink (a bump of one without the other would build a compiler the tree does not name).
[ "$(git -C "$root" rev-parse HEAD:third_party/FEX)" = "$pin" ] || { echo "pin $pin is not the committed gitlink of third_party/FEX" >&2; exit 1; }
git -C "$fex" worktree prune
mkdir "$build"
git -C "$fex" worktree add -q --detach "$build/src" "$pin"
for e in $externals; do
  mkdir -p "$build/src/External/$e"
  git -C "$fex/External/$e" archive "$(git -C "$fex" rev-parse "$pin:External/$e")" | tar x -C "$build/src/External/$e"   # as the pin records it
done
cmake -S "$here" -B "$build/obj" -G Ninja -DFEXC_SRC="$build/src" -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_C_COMPILER=clang -DCMAKE_CXX_COMPILER=clang++
cmake --build "$build/obj" --parallel 4
"$build/obj/fexc_linktest" --init
python3 "$here/smoke.py" "$build/obj/fexc" "$build/smoke"
