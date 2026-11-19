#!/bin/bash
# build.sh: the glibc math campaign image (testkit/inputs.py:campaign_image('libm'); common.sh says how it runs).
# remote.sh's stages sysroot, play, glibc and image libm. The source is the one remote.sh's src stage fetches for it,
# Debian's glibc 2.41-12+deb13u4 through apt-get source -d (the .dsc checked against the signed Sources, the orig and
# Debian tarballs against the .dsc), pinned in sources.sha256. The glibc stage builds it in W/gb and runs
# `make check subdirs=math` in the container through a test-wrapper that logs each test's exact command; libm.py (not
# run by remote.sh) then writes out/libm-cmds.txt and gb-min, the build tree the guest runs those commands against.
# A math test of the native summary (math/subdir-tests.sum) without a recorded command fails the build: every one of
# them is a program the wrapper ran (correctness-userspace.md: 1,859), so a missing one is a broken build. Not programs,
# so not replayed: the four check-* entries (header and ABI checks of the source) and test-tgmath3-macro-list (a
# Python check of gen-tgmath-tests.py's macro list), 1,864 summary entries in all. The container's own verdicts
# (log/libm-native.txt) do not count: Rosetta fails some (fma, long double) that the guest is judged on by itself.
#
# libm-cases.txt: `PART TEST` per program, TEST = NAME as run-libm.sh's CT-RESULT lines name it (the program's path in
# the build tree, math/test-...) and PART the kernel's part= word. run-libm.sh K/N runs entries K, K+N, ... of
# libm-cmds.txt, so the list is made from that file, in its order.
set -euo pipefail
. /recipe/correctness/recipes/common.sh
SUITE=libm
PARTS=1                                               # tests/correctness/family.toml: parts = 1
if [ "${1:-}" != user ]; then root_phase "$(realpath "$0")" "$SUITE"; exit 0; fi

user_env
sysroot
sources 'glibc=2.41-12+deb13u4' libm/sources.sha256
play
stop_if_repin

stage glibc > "$W/log/glibc-stage.log" 2>&1 || { tail -20 "$W/log/glibc-stage.log" "$W/log/glibc-make.log" >&2; exit 1; }
cp "$W/out/libm-native.txt" "$W/log/libm-native.txt"          # the container's own verdicts: kept, not used
python3 -I "$T/libm.py" "$W" > "$W/log/libm-py.log" 2>&1 || { cat "$W/log/libm-py.log" >&2; exit 1; }
cut -f1 "$W/out/libm-cmds.txt" | sort > "$H/cmd-names.txt"
awk '$2 !~ /\/check-/ && $2 != "math/test-tgmath3-macro-list" {print $2}' "$W/out/libm-native.txt" | sort -u > "$H/sum-names.txt"
if [ -n "$(comm -13 "$H/cmd-names.txt" "$H/sum-names.txt")" ]; then
    echo "math tests in the summary without a recorded command:" >&2; comm -13 "$H/cmd-names.txt" "$H/sum-names.txt" | head -20 >&2; exit 1
fi
awk -F'\t' -v n=$PARTS '{print (NR - 1) % n "/" n " " $1}' "$W/out/libm-cmds.txt" > "$W/out/$SUITE-cases.txt"
say "$(wc -l < "$W/out/$SUITE-cases.txt") programs listed ($(wc -l < "$H/sum-names.txt") in the summary)"
stage image "$SUITE" > "$W/log/image.log" 2>&1 || { tail -20 "$W/log/image.log" >&2; exit 1; }
