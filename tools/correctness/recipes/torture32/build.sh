#!/bin/bash
# build.sh: the gcc C torture campaign image for 32-bit code (testkit/inputs.py:campaign_image('torture32'); common.sh
# says how it runs): recipes/torture's tests (the same pinned gcc-14 source, sources.sha256), compiled by the sysroot's
# gcc 14 -m32 -march=i686 (i386.sh's multilib, multilib-debs.sha256; x87 floating point, as Debian's i386 packages),
# -static, at -O0 and -O2 (torture.py build ... ia32). remote.sh's torture stage is repeated here with that compiler.
# A test that does not compile for i686 (__int128 and the like) is left out and listed in log/torture32-not-built.txt.
# The guest's runner is run-torture.sh (the suite's ct word is torture). The campaign compares each program's exit
# status and output with a native run on the x86 reference machine (tests/correctness/torture32.txt).
set -euo pipefail
. /recipe/correctness/recipes/common.sh
. /recipe/correctness/i386.sh
SUITE=torture32
PARTS=1                                               # tests/correctness/family.toml: parts = 1
if [ "${1:-}" != user ]; then root_phase "$(realpath "$0")" "$SUITE"; exit 0; fi

user_env
sysroot
multilib torture32/multilib-debs.sha256
sources 'gcc-14=14.2.0-19' torture32/sources.sha256
play
stop_if_repin

say "building the tests for i686"
export PATH=$W/sysroot/usr/bin:$PATH LD_LIBRARY_PATH=$W/nativelibs
cd "$W"; mkdir -p gcc-t torture/bin
tar xzf src/gcc-14_*.orig.tar.gz -C gcc-t
tar xf gcc-t/*/gcc-*.tar.* -C gcc-t --wildcards '*/gcc/testsuite/gcc.c-torture/execute/*' '*/gcc/testsuite/gcc.dg/tree-ssa/pr105777.c' \
    '*/gcc/testsuite/gcc.dg/tree-ssa/pr109938.c' '*/gcc/testsuite/gcc.dg/tree-ssa/pr109986.c' '*/gcc/testsuite/gcc.dg/tree-ssa/pr30314.c' \
    '*/gcc/testsuite/gcc.dg/pr98304-1.c'
mkdir -p torture/gcc.c-torture; cp -a gcc-t/*/gcc/testsuite/gcc.c-torture/execute torture/gcc.c-torture/
cp -a gcc-t/*/gcc/testsuite/gcc.dg torture/; rm -rf gcc-t
python3 -I "$T/torture.py" build "$W/torture" "$CC32 -march=i686" ia32 > "$W/log/torture-build.log" 2>&1 || { tail -20 "$W/log/torture-build.log" >&2; exit 1; }
cp "$W/torture/commands.txt" "$W/log/torture-commands.txt"; cp "$W/torture/compile-failed.txt" "$W/log/torture32-not-built.txt"
i=0
for c in "$W"/torture/tests-*.tar.zst; do
    zstd -q -dc --long=27 "$c" | tar -t | sort | sed "s|^|$((i % PARTS))/$PARTS |"; i=$((i + 1))
done > "$W/out/$SUITE-cases.txt"
say "$(wc -l < "$W/out/$SUITE-cases.txt") programs listed, $(wc -l < "$W/torture/compile-failed.txt") not built"
mkdir -p "$W/payload/ct"; cp "$W"/torture/tests-*.tar.zst "$W/torture/list.txt" "$W/payload/ct/"
image32 "$SUITE" torture > "$W/log/image.log" 2>&1 || { tail -20 "$W/log/image.log" >&2; exit 1; }
