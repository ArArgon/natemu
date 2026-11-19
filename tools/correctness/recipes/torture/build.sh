#!/bin/bash
# build.sh: the gcc C torture campaign image (testkit/inputs.py:campaign_image('torture'); common.sh says how it runs).
# remote.sh's stages sysroot, torture, play and image torture. The source is the one remote.sh's src stage fetches for
# it, Debian's gcc-14 14.2.0-19 through apt-get source -d, pinned in sources.sha256; the torture stage takes
# gcc.c-torture/execute (with ieee/) and the five gcc.dg files the tests include from its orig tarball, and builds
# each test at -O0 and -O2 (tools/correctness/torture.py). A test that does not compile fails the build: every test
# of this gcc version compiles (correctness-userspace.md), so a missing one is a broken build, not a smaller campaign.
#
# torture-list.txt: `PART TEST` per program, TEST = NAME.OPT as run-torture.sh's CT-RESULT lines name it and PART the
# kernel's part= word. run-torture.sh K/N runs chunks K, K+N, ... of tests-*.tar.zst (200 programs each), so the list
# is read from the chunks the image's /ct holds, in run-torture.sh's order.
set -euo pipefail
. /recipe/correctness/recipes/common.sh
SUITE=torture
PARTS=1                                               # tests/correctness/family.toml: parts = 1
if [ "${1:-}" != user ]; then root_phase "$(realpath "$0")" "$SUITE"; exit 0; fi

user_env
sysroot
sources 'gcc-14=14.2.0-19' torture/sources.sha256
play
stop_if_repin

stage torture > "$W/log/torture-stage.log" 2>&1 || { tail -20 "$W/log/torture-stage.log" "$W/log/torture-build.log" >&2; exit 1; }
if [ -s "$W/torture/compile-failed.txt" ]; then
    echo "$(wc -l < "$W/torture/compile-failed.txt") torture tests did not compile:" >&2; grep compile-failed "$W/log/torture-build.log" | head -20 >&2; exit 1
fi
cp "$W/torture/commands.txt" "$W/log/torture-commands.txt"
i=0
for c in "$W"/torture/tests-*.tar.zst; do
    zstd -q -dc --long=27 "$c" | tar -t | sort | sed "s|^|$((i % PARTS))/$PARTS |"; i=$((i + 1))
done > "$W/out/$SUITE-list.txt"
[ "$(wc -l < "$W/out/$SUITE-list.txt")" = "$(wc -l < "$W/torture/list.txt")" ] || { echo "the chunks do not hold list.txt's tests" >&2; exit 1; }
say "$(wc -l < "$W/out/$SUITE-list.txt") programs listed"
stage image "$SUITE" > "$W/log/image.log" 2>&1 || { tail -20 "$W/log/image.log" >&2; exit 1; }
