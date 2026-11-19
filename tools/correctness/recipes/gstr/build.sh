#!/bin/bash
# build.sh: the glibc string and wide-character campaign image (testkit/inputs.py:campaign_image('gstr'); common.sh says
# how it runs). remote.sh's stages sysroot and play, then cpy-remote.sh's glibc and imgg. The source is Debian's glibc
# 2.41-12+deb13u4 through apt-get source -d, pinned in sources.sha256 (the same files as libm's). cpy-remote.sh's glibc
# stage builds it in W/gb, runs `make check subdirs=string` and `subdirs=wcsmbs` in the container through a logging
# test-wrapper and runs gstr.py, which writes out/gstr-cmds.txt and gb-min (the build tree the guest runs them against).
# A test of the native summaries without a recorded command fails the build, except what gstr.py does not replay by
# design (correctness-cpython-string.md, "glibc string and wide-character tests"): the six check-* header checks,
# string/tst-svc-cmp (gstr.py folds its cmp of tst-svc's output, and tst-svc's stdin, into string/tst-svc's command) and
# the two tests without a test program (tst-strerror, tst-strsignal: UNSUPPORTED in the summary).
#
# gstr-cases.txt: `PART TEST` per program, TEST = NAME as run-gstr.sh's CT-RESULT lines name it (string/... or
# wcsmbs/...) and PART the kernel's part= word. run-gstr.sh K/N runs entries K, K+N, ... of gstr-cmds.txt, so the list
# is made from that file, in its order.
set -euo pipefail
. /recipe/correctness/recipes/common.sh
SUITE=gstr
PARTS=6                                               # tests/correctness/family.toml: parts = 6
if [ "${1:-}" != user ]; then root_phase "$(realpath "$0")" "$SUITE"; exit 0; fi

user_env
sysroot
sources 'glibc=2.41-12+deb13u4' gstr/sources.sha256
play
stop_if_repin

cpy() { say "cpy-remote.sh $*"; bash "$T/cpy-remote.sh" "$W" "$@"; }
cpy glibc > "$W/log/glibc-stage.log" 2>&1 || { tail -20 "$W/log/glibc-stage.log" "$W/log/glibc-make.log" >&2; exit 1; }
cp "$W"/out/gstr-native-sum-*.txt "$W/log/"                     # the container's own verdicts: kept, not used
cut -f1 "$W/out/gstr-cmds.txt" | sort > "$H/cmd-names.txt"
cat "$W"/out/gstr-native-sum-*.txt | awk '$2 !~ /\/check-/ && $2 != "string/tst-svc-cmp" && $2 != "string/tst-strerror" && $2 != "string/tst-strsignal" {print $2}' \
    | sort -u > "$H/sum-names.txt"
if [ -n "$(comm -13 "$H/cmd-names.txt" "$H/sum-names.txt")" ]; then
    echo "tests in the summaries without a recorded command:" >&2; comm -13 "$H/cmd-names.txt" "$H/sum-names.txt" | head -20 >&2; exit 1
fi
awk -F'\t' -v n=$PARTS '{print (NR - 1) % n "/" n " " $1}' "$W/out/gstr-cmds.txt" > "$W/out/$SUITE-cases.txt"
say "$(wc -l < "$W/out/$SUITE-cases.txt") programs listed"
cpy imgg > "$W/log/image.log" 2>&1 || { tail -20 "$W/log/image.log" >&2; exit 1; }
