#!/bin/bash
# build.sh: the x86-TSO litmus campaign image (testkit/inputs.py:campaign_image('litmus'); common.sh says how it runs).
# remote.sh's stages sysroot and play, then x86-remote.sh's ocaml, herd, litmus and image litmus
# (docs/restructure/correctness-x86-litmus.md, suite 2), with the sources x86-remote.sh expects in src/:
#   herdtools7 7.58         the GitHub tag archive (src/herdtools7-7.58.tar.gz);
#   its catalogue           export-ignored from that archive, so each catalogue/x86{,_64}/tests/*.litmus file of the
#                           tag's commit (1ca343e1) from raw.githubusercontent.com, into src/herd-git/catalogue (the
#                           files to fetch are the ones sources.sha256 names);
#   litmus-tests-x86        the GitHub archive of commit a6dadce (src/ltx86);
# all pinned in sources.sha256. The OCaml 5.3 toolchain herd is built with (Debian trixie's OCaml, dune, menhir,
# zarith, findlib) is x86-remote.sh's ocaml stage, downloaded through remote.sh's snapshot apt and pinned in
# ocaml-debs.sha256. herd7 and litmus7 are built in the container, the corpus is litmus-gen.py's (1,318 tests in
# cat86, cat64, ltx2/3/4/co, rmw and lk-add/inc/xor/xchg/cmpxchg), and herd7 writes the oracle (oracle/FAMILY.txt: the
# x86-TSO final states of every test). The harnesses are litmus7 -a 4 -s 100000 -r 100, dynamic against Debian libc.
#
# Outputs besides common.sh's: /work/oracle/FAMILY.txt, which campaign_image keeps in the image's folder
# (tests/correctness/verdicts.py:litmus reads it), and /ct/litmus-cases.txt in the image.
#
# litmus-cases.txt: `PART TEST` per harness, TEST = FAMILY.NAME as run-litmus.sh's CT-RESULT lines name it and PART
# the kernel's part= word. run-litmus.sh K/N runs the K-th, (K+N)-th, ... file of `ls /ct/lit | sort` (threads.txt
# left out), so the list is made the same way from the same folder.
set -euo pipefail
. /recipe/correctness/recipes/common.sh
SUITE=litmus
PARTS=2                                               # tests/correctness/family.toml: parts = 2
HERD_COMMIT=1ca343e16a2038e406d1ac674e7e3a1b722b36c7  # herdtools7 tag 7.58
LTX=a6dadced36343b9c21f00a173950a23e90068cd0          # litmus-tests-x86
HERD_URL=https://github.com/herd/herdtools7/archive/refs/tags/7.58.tar.gz
RAW=https://raw.githubusercontent.com/herd/herdtools7/$HERD_COMMIT
LTX_URL=https://github.com/litmus-tests/litmus-tests-x86/archive/$LTX.tar.gz
if [ "${1:-}" != user ]; then
    # ocamlopt links with x86_64-linux-gnu-gcc, which the ocaml stage points at /usr/bin/cc (the reference machine's
    # gcc). The builder has no compiler: /usr/bin/cc is the sysroot's Debian gcc 14, as every other recipe compiles.
    [ -e /usr/bin/cc ] || printf '#!/bin/sh\nexport PATH=%s/sysroot/usr/bin:$PATH LD_LIBRARY_PATH=%s/nativelibs\nexec x86_64-linux-gnu-gcc-14 --sysroot=%s/sysroot "$@"\n' \
        "$W" "$W" "$W" > /usr/bin/cc
    chmod 755 /usr/bin/cc
    root_phase "$(realpath "$0")" "$SUITE"
    mkdir -p /work/oracle; cp "$W"/oracle/*.txt /work/oracle/
    exit 0
fi

x86() { say "x86-remote.sh $*"; bash "$T/x86-remote.sh" "$W" "$@"; }

# github: the herdtools7 archive and catalogue, the litmus-tests-x86 archive, checked against sources.sha256, into src/.
github() {
    local d=$H/gh f n a
    rm -rf "$d" "$H/gh.txt"; mkdir "$d"
    get() {
        say "curl $1"
        curl -sSfL --retry 5 --retry-delay 5 --connect-timeout 30 --max-time 900 -o "$d/$2" "$1" || { echo "Failed to fetch $1" >&2; exit 1; }
        echo "$1 $(sha256sum < "$d/$2" | cut -d' ' -f1) $2" >> "$H/gh.txt"
    }
    get "$HERD_URL" herdtools7-7.58.tar.gz
    get "$LTX_URL" "litmus-tests-x86-$LTX.tar.gz"
    for f in $(awk '$2 ~ /^herdtools7-catalogue-/ {print $2}' "$RC/litmus/sources.sha256"); do
        n=${f#herdtools7-catalogue-}; case $n in x86_64-*) a=x86_64;; *) a=x86;; esac
        get "$RAW/catalogue/$a/tests/${n#"$a"-}" "$f"
    done
    (cd "$d" && sha256sum -- *) > "$H/gh.lst"; pins "$H/gh.lst" litmus/sources.sha256
    cat "$H/gh.txt" >> "$W/out/downloads.txt"
    mkdir -p "$W/src/herd-git/catalogue/x86/tests" "$W/src/herd-git/catalogue/x86_64/tests" "$W/src/ltx86"
    cp "$d/herdtools7-7.58.tar.gz" "$W/src/"
    for f in "$d"/herdtools7-catalogue-*; do
        n=${f##*/herdtools7-catalogue-}; case $n in x86_64-*) a=x86_64;; *) a=x86;; esac
        cp "$f" "$W/src/herd-git/catalogue/$a/tests/${n#"$a"-}"
    done
    tar xzf "$d/litmus-tests-x86-$LTX.tar.gz" -C "$W/src/ltx86" --strip-components=1
    say "catalogue: $(ls "$W/src/herd-git/catalogue/x86/tests" | wc -l) x86, $(ls "$W/src/herd-git/catalogue/x86_64/tests" | wc -l) x86_64 tests"
}

user_env
sysroot
play
github
# The ocaml stage looks for each package under its index name (libgmp-dev_6.3.0+dfsg-3_amd64.deb) and downloads it
# when absent, but apt-get download names a package with an epoch 2%3a6.3.0+dfsg-3: the check then fails. The packages
# (those ocaml-debs.sha256 names, the stage's list) are downloaded here first, pinned under apt's names, and handed
# to the stage under the index names.
rm -rf "$H/ocpre"; mkdir -p "$H/ocpre" "$W/ocdebs"
(cd "$H/ocpre" && apt-get download -qq $(awk '{split($2, a, "_"); print a[1]}' "$RC/litmus/ocaml-debs.sha256") >/dev/null)
debsums "$H/ocpre" > "$H/ocaml.lst"; pins "$H/ocaml.lst" litmus/ocaml-debs.sha256
uris "$H"/ocpre/*.deb >> "$W/out/downloads.txt"
for f in "$H"/ocpre/*.deb; do b=${f##*/}; cp "$f" "$W/ocdebs/$(echo "$b" | sed 's/_[0-9]*%3a/_/')"; done
x86 ocaml > "$W/log/ocaml.log" 2>&1 || { tail -20 "$W/log/ocaml.log" >&2; exit 1; }
stop_if_repin

x86 herd > "$W/log/herd-stage.log" 2>&1 || { tail -30 "$W/log/herd-stage.log" "$W/log/herd.log" >&2; exit 1; }
x86 litmus > "$W/log/litmus-stage.log" 2>&1 || { tail -30 "$W/log/litmus-stage.log" >&2; exit 1; }
mkdir -p "$W/log/oracle-err"; cp "$W"/oracle/*.err "$W/log/oracle-err/"
if grep -q 'make errors' "$W/log/litmus-stage.log"; then
    echo "litmus7 harnesses did not compile:" >&2; grep 'make errors' "$W/log/litmus-stage.log" >&2; exit 1
fi
ls "$W/lit" | grep -v '^threads.txt$' | LC_ALL=C sort > "$H/lit.lst"
[ "$(wc -l < "$H/lit.lst")" = "$(wc -l < "$W/corpus/threads.txt")" ] ||
    { echo "$(wc -l < "$H/lit.lst") harnesses for $(wc -l < "$W/corpus/threads.txt") corpus tests" >&2; exit 1; }
# Every harness needs herd7's states (verdicts.py fails a test without them): the names herd7's oracle gives.
for f in "$W"/oracle/*.txt; do b=${f##*/}; awk -v F="${b%.txt}" '/^Test / {print F "." $2}' "$f"; done | LC_ALL=C sort > "$H/oracle.lst"
LC_ALL=C comm -3 "$H/lit.lst" "$H/oracle.lst" > "$W/log/oracle-mismatch.txt"
say "$(wc -l < "$H/lit.lst") harnesses, $(wc -l < "$H/oracle.lst") oracle tests, $(wc -l < "$W/log/oracle-mismatch.txt") names in only one of them"
[ ! -s "$W/log/oracle-mismatch.txt" ] ||
    { echo "harness and oracle names differ (log/oracle-mismatch.txt):" >&2; head -20 "$W/log/oracle-mismatch.txt" >&2; exit 1; }

i=0
while read -r f; do echo "$((i % PARTS))/$PARTS $f"; i=$((i + 1)); done < "$H/lit.lst" > "$W/out/$SUITE-cases.txt"
say "$(wc -l < "$W/out/$SUITE-cases.txt") tests listed"
x86 image "$SUITE" > "$W/log/image.log" 2>&1 || { tail -20 "$W/log/image.log" >&2; exit 1; }
overlay "$W/out/$SUITE.cpio.zst" "$W/out/$SUITE-ct.cpio.zst" "$W/out/$SUITE-cases.txt"
mv "$W/out/$SUITE-ct.cpio.zst" "$W/out/$SUITE.cpio.zst"
