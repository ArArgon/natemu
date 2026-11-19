#!/bin/bash
# build.sh: the kernel x86 selftest campaign image (testkit/inputs.py:campaign_image('x86st'); common.sh says how it
# runs). remote.sh's stages sysroot and play, then x86-remote.sh's selftests and image x86st. The source is Debian's
# linux 6.12.107-1 (the guest kernel's source package) through apt-get source -d, pinned in sources.sha256; the
# selftests stage takes tools/testing/selftests/x86 from its orig tarball (no Debian patch touches tools/testing) and
# builds the 64-bit programs -static (correctness-x86-litmus.md, "Suite 1": 25 programs). Another count fails the
# build: the suite boots one program per part (parts = 25).
#
# x86st-cases.txt: `PART TEST` per program, TEST = the program's file name as run-x86st.sh's CT-RESULT (or CT-SKIP)
# lines name it and PART the kernel's part= word. run-x86st.sh K/N runs programs K, K+N, ... of `ls /ct/x86st | sort`,
# so the list is made from the programs the image's /ct/x86st holds, in that order. A program named in
# tools/correctness/x86st-skip.txt (also in /ct) is listed too: run-x86st.sh reports it with CT-SKIP.
set -euo pipefail
. /recipe/correctness/recipes/common.sh
SUITE=x86st
PARTS=25                                              # tests/correctness/family.toml: parts = 25
if [ "${1:-}" != user ]; then root_phase "$(realpath "$0")" "$SUITE"; exit 0; fi

user_env
sysroot
sources 'linux=6.12.107-1' x86st/sources.sha256
play
stop_if_repin

x86() { say "x86-remote.sh $*"; bash "$T/x86-remote.sh" "$W" "$@"; }
x86 selftests > "$W/log/selftests-stage.log" 2>&1 || { tail -20 "$W/log/selftests-stage.log" "$W/log/x86st-make.log" >&2; exit 1; }
(cd "$W/x86st" && ls -- *_64) | sort > "$H/x86st.txt"
if [ "$(wc -l < "$H/x86st.txt")" != $PARTS ]; then
    echo "$(wc -l < "$H/x86st.txt") selftest programs built, not $PARTS:" >&2; tr '\n' ' ' < "$H/x86st.txt" >&2
    grep -E 'error|build failed' "$W/log/x86st-make.log" | head -20 >&2; exit 1
fi
awk -v n=$PARTS '{print (NR - 1) % n "/" n " " $1}' "$H/x86st.txt" > "$W/out/$SUITE-cases.txt"
say "$(wc -l < "$W/out/$SUITE-cases.txt") programs listed"
x86 image "$SUITE" > "$W/log/image.log" 2>&1 || { tail -20 "$W/log/image.log" >&2; exit 1; }
