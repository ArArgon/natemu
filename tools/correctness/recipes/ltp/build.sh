#!/bin/bash
# build.sh: the LTP campaign image (testkit/inputs.py:campaign_image('ltp'); common.sh says how it runs).
# remote.sh's stages sysroot, ltp, play and image ltp. The source is the one remote.sh's src stage fetches for LTP
# (not packaged by Debian): upstream's ltp-full-20250530 release, checked against sources.sha256 and remote.sh's own
# pin (tools/correctness/ltp.sha256). The src stage itself is not run: its other sources (gcc-14, glibc) are other
# suites'. The ltp stage builds it with Debian gcc against Debian libc (dynamic; libc6 is in the play rootfs) and
# leaves out listmount04 (its headers are newer than trixie's).
#
# ltp-list.py (over the installed runtest files and the source's testcases) writes ltp-list.txt, run-ltp.sh's input
# (RUNTEST<TAB>NAME<TAB>COMMAND), ltp-excluded.txt (what is left out, with reasons) and ltp-keep.txt (the
# testcases/bin files the image carries). docs/restructure/correctness-userspace.md's results are for this list: 1,446
# entries run, 256 excluded; a build that lists anything else stops. Both lists are kept in log/.
#
# ltp-cases.txt: `PART TEST` per entry, TEST = RUNTEST:NAME as run-ltp.sh's CT-RESULT lines name it and PART the
# kernel's part= word (cases.sh: run-ltp.sh K/N runs entries K, K+N, ...).
set -euo pipefail
. /recipe/correctness/recipes/common.sh
SUITE=ltp
PARTS=4                                               # tests/correctness/family.toml: parts = 4
LTP=ltp-full-20250530
URL=https://github.com/linux-test-project/ltp/releases/download/20250530/$LTP.tar.xz
if [ "${1:-}" != user ]; then root_phase "$(realpath "$0")" "$SUITE"; exit 0; fi

user_env
sysroot
say "curl $URL"
mkdir -p "$W/src"
curl -sSLf --retry 5 -o "$W/src/$LTP.tar.xz" "$URL" || { echo "Failed to fetch $URL" >&2; exit 1; }
(cd "$W/src" && sha256sum -- "$LTP.tar.xz") > "$H/src.lst"; pins "$H/src.lst" ltp/sources.sha256
echo "$(cat "$T/ltp.sha256")  $W/src/$LTP.tar.xz" | sha256sum -c --quiet
echo "$URL $(sha256sum < "$W/src/$LTP.tar.xz" | cut -d' ' -f1) $LTP.tar.xz" >> "$W/out/downloads.txt"
play
stop_if_repin

stage ltp > "$W/log/ltp-stage.log" 2>&1 || { tail -20 "$W/log/ltp-stage.log" "$W/log/ltp-make.log" >&2; exit 1; }
cat "$W/log/ltp-stage.log" >&2
# ltp-list.py reads the sources' testcases (their tst_test markers); the ltp stage removed its source tree.
rm -rf "$H/ltp-list-src"; mkdir "$H/ltp-list-src"
tar xJf "$W/src/$LTP.tar.xz" -C "$H/ltp-list-src" --strip-components=1 "$LTP/testcases"
python3 -I "$T/ltp-list.py" "$W/ltp-inst/opt/ltp" "$H/ltp-list-src" "$W/out" | tee "$W/log/ltp-list.log" >&2
rm -rf "$H/ltp-list-src"
cp "$W/out/ltp-list.txt" "$W/out/ltp-excluded.txt" "$W/out/ltp-keep.txt" "$W/log/"
listed=$(wc -l < "$W/out/ltp-list.txt"); excluded=$(wc -l < "$W/out/ltp-excluded.txt")
[ "$listed" = 1446 ] && [ "$excluded" = 256 ] || {
    echo "ltp-list.py listed $listed entries and excluded $excluded: correctness-userspace.md's results are for 1,446 and 256" >&2; exit 1; }
# Every entry's program was built, except listmount04 (left out by the ltp stage; it reports exit 127). The program
# is the first word of the command's last `;` part (mmapstress07 and rwtest05 set a variable first).
absent=$(awk -F'\t' '{n = split($3, s, ";"); split(s[n], c, " "); print c[1]}' "$W/out/ltp-list.txt" | sort -u |
         while read -r p; do [ -e "$W/ltp-inst/opt/ltp/testcases/bin/$p" ] || echo "$p"; done | tr '\n' ' ')
[ "$absent" = "listmount04 " ] || { echo "LTP programs not built: $absent(see log/ltp-make.log)" >&2; exit 1; }
sh "$RC/ltp/cases.sh" "$W/out/ltp-list.txt" "$PARTS" > "$W/out/$SUITE-cases.txt"
say "$(wc -l < "$W/out/$SUITE-cases.txt") entries listed"
stage image "$SUITE" > "$W/log/image.log" 2>&1 || { tail -20 "$W/log/image.log" >&2; exit 1; }
