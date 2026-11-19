#!/bin/bash
# build.sh: the CPython campaign images, sourced by recipes/cpyt/build.sh (SUITE=cpyt, the regression tests) and
# recipes/cpyb/build.sh (SUITE=cpyb, the in-guest build); testkit/inputs.py:campaign_image(SUITE) runs those, and
# common.sh says how. tools/correctness/cpy-remote.sh's stages, unchanged, with W=/home/ubuntu/cp (cpy-remote.sh's
# WORKDIR on the reference machine: run-cpy.sh's PY=/home/ubuntu/cp/py and run-cpyb.sh's W are that path):
#   remote.sh sysroot and play; Debian's python3.13 3.13.5-2+deb13u5 through apt-get source -d (the .dsc checked
#   against the signed Sources, the orig and Debian tarballs against the .dsc), pinned in cpy/sources.sha256;
#   cpy-remote.sh deps (CPython's build dependencies, pinned in cpy/deps-debs.sha256) and pysrc (orig + Debian's patch
#   series, mtimes uniform); then
#   cpyt: cpy-remote.sh pybuild (cpy-build.sh, make -j4) and imgt (the build tree minus objects);
#   cpyb: cpy-remote.sh imgb (the pristine source; the guest builds it).
#
# cpy-tests.txt (run-cpy.sh's `TEST PART [TAG...]` lines, in the image's /ct and in the entry's log/): every module
# the built interpreter's `python -m test --list-tests` names (regrtest splits its package tests, test_asyncio and
# the like, into their modules and names those `test.test_asyncio.test_x`: kept as named, run-cpy.sh and its
# CT-RESULT lines use the same names), in that order, module i in part i % PARTS; `smoke` tags the modules of cpy/smoke.txt,
# the tests run-cpyb.sh runs with the guest-built interpreter. The cpyb image holds only the smoke lines.
#
# SUITE-cases.txt (`PART TEST`, PART the kernel's part= word):
#   cpyt: `K/12 MODULE` per module of part K: run-cpy.sh's CT-RESULT cpy lines name the module;
#   cpyb: `smoke configure`, `smoke make` (cpy-build.sh's CT-RESULT cpyb steps), then `smoke MODULE` per smoke module.
set -euo pipefail
. /recipe/correctness/recipes/common.sh
: "${SUITE:?source this through recipes/cpyt/build.sh or recipes/cpyb/build.sh}"
PARTS=12                                              # tests/correctness/family.toml: cpyt's parts = 12
W=$H/cp                                               # cpy-remote.sh's WORKDIR (common.sh's default is $H/ct)
if [ "${1:-}" != user ]; then root_phase "$(realpath "$0")" "$SUITE"; exit 0; fi

cstage() { say "cpy-remote.sh $*"; bash "$T/cpy-remote.sh" "$W" "$@"; }
fail() { tail -n 30 "$@" >&2; exit 1; }

user_env
sysroot
sources 'python3.13=3.13.5-2+deb13u5' cpy/sources.sha256
play
cstage deps > "$W/log/cpy-deps.log" 2>&1 || fail "$W/log/cpy-deps.log"
sed 's| \./| |' "$W/out/cpy-deps-debs.sha256" > "$H/deps.lst"; pins "$H/deps.lst" cpy/deps-debs.sha256
uris "$W"/debs-deps/*.deb >> "$W/out/downloads.txt"
stop_if_repin

cstage pysrc > "$W/log/cpy-pysrc.log" 2>&1 || fail "$W/log/cpy-pysrc.log" "$W/log/py-patch.log"
SMOKE=$T/recipes/cpy/smoke.txt
case $SUITE in
cpyt)
    cstage pybuild > "$W/log/cpy-pybuild.log" 2>&1 || fail "$W/log/cpy-pybuild.log" "$W/out/native-cpyb.txt"
    cp "$W/out/native-cpyb.txt" "$W/log/"; cp -r "$W/pylog" "$W/log/pylog"
    grep -q '^CT-RESULT cpyb make rc=0 ' "$W/out/native-cpyb.txt" || fail "$W/out/native-cpyb.txt"
    (cd "$W/py" && LD_LIBRARY_PATH=$W/nativelibs ./python -m test --list-tests) > "$W/log/list-tests.txt" 2>&1 \
        || fail "$W/log/list-tests.txt"
    grep -E '^(test\.)?test_[A-Za-z0-9_.]+$' "$W/log/list-tests.txt" > "$H/modules.txt"
    awk -v p="$PARTS" 'NR == FNR { if ($1 !~ /^#/ && NF) s[$1] = 1; next }
                       { printf "%s %d%s\n", $1, (FNR - 1) % p, ($1 in s) ? " smoke" : "" }' "$SMOKE" "$H/modules.txt" \
        > "$W/out/cpy-tests.txt"
    awk '{print $2 "/'"$PARTS"' " $1}' "$W/out/cpy-tests.txt" | sort -t/ -k1,1n -s > "$W/out/$SUITE-cases.txt"
    ;;
cpyb)
    awk '$1 !~ /^#/ && NF {print $1 " smoke"}' "$SMOKE" > "$W/out/cpy-tests.txt"
    { echo 'smoke configure'; echo 'smoke make'; awk '{print "smoke " $1}' "$W/out/cpy-tests.txt"; } > "$W/out/$SUITE-cases.txt"
    ;;
*) echo "unknown SUITE $SUITE" >&2; exit 1;;
esac
if [ "$SUITE" = cpyt ]; then                          # a smoke module the interpreter does not list is noted (log/)
    awk '$1 !~ /^#/ && NF {print $1}' "$SMOKE" | grep -vxFf "$H/modules.txt" > "$W/log/smoke-unlisted.txt" || true
    [ ! -s "$W/log/smoke-unlisted.txt" ] || say "cpy/smoke.txt names modules regrtest does not list: $(cat "$W/log/smoke-unlisted.txt")"
fi
cp "$W/out/cpy-tests.txt" "$W/log/"
say "$(wc -l < "$W/out/cpy-tests.txt") modules, $(wc -l < "$W/out/$SUITE-cases.txt") tests listed"
img=imgt; [ "$SUITE" = cpyb ] && img=imgb
cstage "$img" > "$W/log/cpy-$img.log" 2>&1 || fail "$W/log/cpy-$img.log"
cp "$W/out/$SUITE-cases.txt" "$W/log/"
