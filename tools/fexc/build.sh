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
pin=39eb0db56fa9de756b73063c934d877941519883   # == third_party/FEX gitlink: bulk REP MOVSB and MOVSQ (FexcRepMovs; the bulk REP plan's first cut) and CLAUDE.md's pointer to natemu's AGENTS.md, on 13a1bfc; 13a1bfc is M2 checkpoint C (32-bit displacements and stack writeback folded as before, under the 4 GiB compat root; 32-bit SHLD/SHRD results 32 bits wide; one operand check per instruction through a limited FS or GS, FexcSegmentBase) on 4eba851; 4eba851 is every 32-bit guest-RIP move is movz w; movk w, lsl 16; nop (32-bit code shared position-independent) on 9554e5b; 9554e5b is a unit-starting FISTP m64's own RIP is a guest-RIP relocation (32-bit code reuse) on b89eb4c; b89eb4c is an exit to a plain constant is a computed exit, not a guest-RIP relocation (32-bit code reuse) on a75d79f; a75d79f is the FLD m64 without its multiply sets no DE without AFP (fast x87 without AFP) on cdc9ea8; cdc9ea8 is a split stub may probe (FexcProbes; M2 task 4) on 54d1309; 54d1309 is the int64 pair only where the stack check is a stub (review of the pair) on 4332a65; 4332a65 is FLD m64 without a run in a unit with no computing x87 instruction (M2 task 4) on 8bbe223; 8bbe223 is the integer kept for a faulted FISTP m64 of the pair (M2 task 4) on 5806f25; 5806f25 is FILD m64 then FISTP m64 in one block stores the loaded integer (M2 task 4) on 8c1633f; 8c1633f is a restarted JIT attempt keeps no x87 map, slot or hold of the one before (review of the map) on ea9e724; ea9e724 is the review of the guess (its argument read for 32-bit code only; the inline hold after BeginBlock) on 3f6ccaa; 3f6ccaa is no x87 state written before an instruction that can fault under a known stack top (FexcX87Slot, FexcX87Map; M2 task 4) on c24d00b; c24d00b is the review of the x87 caches (Mutated after an op that flushes inside an instruction; the inline hold in an IR block that begins inside one) on efd78b8; efd78b8 is a unit of 32-bit code compiled for the x87 stack top and tags of its request (--x87-guess, FexcX87Guess; M2 task 4) on ed70438; ed70438 is the x87 stack in host registers across a block, its stack check as a stub, lazy x87 flags (M2 task 4) on 41d690b; 41d690b is FLDCW in translated code (FexcFldcw; M2 task 4) on 2c58353; 2c58353 is dba049b + the 32-bit string step (ESI/EDI/ECX in 32 bits, ES:EDI; M2 task 2); dba049b is ac479dd + fexc_ext_abandon (an over-long extended unit stops before the passes) and PoolBufferKeepClaimed; ac479dd is daa67e4 + the translated-code options (--ext-conds extended blocks, --push-run, --narrow-cmp; fexc_hook.h); daa67e4 is 6d2f5d8 + the SIMD DE screen's hoist hooks (FexcHoistPlan/FexcHoistEmit; the shim does the rest); 6d2f5d8 is 275de77 + FexcFmaCheck (AFP accurate chunk 4); 275de77 is 93350e2 + the NaN-sign-keeping negations; 93350e2 (720fe1c (807033d (791857f (726f3c0 (8254266 (7e8f5ed (13e80cf (1df70f0 + non-AFP MINSS/MAXSS signalling compare) + FexcFlush) + FexcSimdCheckDe/FexcX87CheckDe (AFP fallback 3b)) + non-AFP scalar direct write (MarkUpperZero)) + FexcKeep (unmasked UE tininess) + the FexcX87NanOp declaration (x87 NaN fix-up)) + FexcSimdDe (accurate masked SSE/AVX DE)) + FexcSimdSkip/FexcSimdSkipEnd (its screen)) + FexcSimdSkip without a result register) + NaN-sign-keeping negations without AFP (cached multipliers for the packed FMA addend negations, ADDSUB as fmul and fadd)
# FEXC_PIN and FEXC_TOOLS (testkit's fexc_at): another revision's compiler, from its FEX pin and a copy of its tools/fexc;
# the pin is then not this checkout's gitlink, and that copy's smoke check (which reads the checkout around it) is not run.
other=${FEXC_PIN:+1}
pin=${FEXC_PIN:-$pin}
tools=$here
[ -z "$other" ] || tools=${FEXC_TOOLS:?FEXC_PIN needs FEXC_TOOLS}
externals="fmt xxhash unordered_dense"
# A symlinked third_party/FEX (a git worktree sharing the main checkout's initialised submodule) is used as it is: git refuses to
# update a submodule path that is a symlink. The pin check below still applies.
# So is a third_party/FEX that is a linked git worktree of the main checkout's submodule (its git dir is not the
# submodule's common one): `git submodule update` there would rewrite the shared module's core.worktree to this checkout
# and break the main checkout's FEX. The externals then come from the submodule's own module stores (below).
linked=
[ -L "$root/third_party/FEX" ] || [ "$(cd "$fex" 2>/dev/null && git rev-parse --absolute-git-dir 2>/dev/null)" = "$(cd "$fex" 2>/dev/null && cd "$(git rev-parse --git-common-dir 2>/dev/null)" 2>/dev/null && pwd -P)" ] || linked=1
if [ ! -L "$root/third_party/FEX" ] && [ -z "$linked" ]; then
  git -C "$root" submodule update --init --depth 1 third_party/FEX
  for e in $externals; do git -C "$fex" submodule update --init --depth 1 External/$e; done
fi
# A shared checkout may sit on another branch: the pin only has to exist in it (the build is a detached worktree at the pin).
{ [ -L "$root/third_party/FEX" ] || [ -n "$other" ] || [ "$(git -C "$fex" rev-parse HEAD)" = "$pin" ]; } && git -C "$fex" cat-file -e "$pin^{commit}" ||
  { echo "third_party/FEX is not at the pinned commit $pin" >&2; exit 1; }
# The pin is the superproject's gitlink (a bump of one without the other would build a compiler the tree does not name).
[ -n "$other" ] || [ "$(git -C "$root" rev-parse HEAD:third_party/FEX)" = "$pin" ] || { echo "pin $pin is not the committed gitlink of third_party/FEX" >&2; exit 1; }
git -C "$fex" worktree prune
mkdir "$build"
git -C "$fex" worktree add -q --detach "$build/src" "$pin"
for e in $externals; do
  mkdir -p "$build/src/External/$e"
  if [ -n "$linked" ]; then extgit="--git-dir=$(cd "$fex" && cd "$(git rev-parse --git-common-dir)" && pwd -P)/modules/External/$e"; else extgit="-C $fex/External/$e"; fi
  git $extgit archive "$(git -C "$fex" rev-parse "$pin:External/$e")" | tar x -C "$build/src/External/$e"   # as the pin records it
done
cmake -S "$tools" -B "$build/obj" -G Ninja -DFEXC_SRC="$build/src" -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_C_COMPILER=clang -DCMAKE_CXX_COMPILER=clang++
cmake --build "$build/obj" --parallel 4
"$build/obj/fexc_linktest" --init
[ -n "$other" ] || python3 "$here/smoke.py" "$build/obj/fexc" "$build/smoke"
