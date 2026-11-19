#!/bin/bash
# build.sh: the stress-ng campaign image (testkit/inputs.py:campaign_image('stress'); common.sh says how it runs).
# remote.sh's sysroot and play stages, then stress-remote.sh's build and image stages, unchanged: Debian trixie's
# stress-ng 0.19.02-1 through apt-get source -d (the .dsc checked against the signed Sources, the tarballs against the
# .dsc), pinned in sources.sha256 (the same three files tools/io/stress-ng pins), with Debian's patch series, built
# `make STATIC=1` with Debian gcc 14 against Debian libc 2.41. The binary is not pinned: its sha256 goes to
# log/built.sha256, which the image's manifest.json lists under `built`. The image is stress-remote.sh's (the play rootfs, the correctness /init and
# /ct/run-stress.sh, run-repro.sh, stress-list.txt, stress-ng), plus /ct/stress-cases.txt as an appended cpio.
#
# stress-cases.txt: `PART TEST` per stressor run (cases.py, from the same stress-list.txt the image's /ct holds):
# PART the `FAMILY,seed=1,n=4` word run-stress.sh takes, TEST its CT-RESULT name STRESSOR[:LABEL].r1.
set -euo pipefail
. /recipe/correctness/recipes/common.sh
SUITE=stress
PARTS=18                                              # tests/correctness/family.toml: parts = 18
if [ "${1:-}" != user ]; then root_phase "$(realpath "$0")" "$SUITE"; exit 0; fi

user_env
sysroot
sources 'stress-ng=0.19.02-1' stress/sources.sha256
play
stop_if_repin

# stress-remote.sh build reports the binary with file(1), which the builder image lacks: a stand-in naming the ELF class.
file() { echo "$1: $(head -c 5 "$1" | od -An -c | tr -s ' ')"; }
export -f file
# Without common.sh's SOURCE_DATE_EPOCH: stress-ng's Makefile turns it into -DHAVE_SOURCE_DATE_EPOCH, which the
# earlier stress-ng builds (53140a75...) did not have.
say "stress-remote.sh build"
env -u SOURCE_DATE_EPOCH bash "$T/stress-remote.sh" "$W" build > "$W/log/stress-build.log" 2>&1 || { tail -20 "$W/log/stress-build.log" "$W/log/stress-make.log" >&2; exit 1; }
(cd "$W/st" && sha256sum stress-ng) > "$W/log/built.sha256"; say "built $(cat "$W/log/built.sha256")"

python3 /recipe/correctness/recipes/stress/cases.py "$T/stress-list.txt" > "$W/out/$SUITE-cases.txt"
n=$(cut -d' ' -f1 "$W/out/$SUITE-cases.txt" | uniq | wc -l)
[ "$n" = "$PARTS" ] || { echo "stress-cases.txt has $n parts, not $PARTS" >&2; exit 1; }
say "$(wc -l < "$W/out/$SUITE-cases.txt") stressor runs listed in $n parts"
say "stress-remote.sh image"
bash "$T/stress-remote.sh" "$W" image > "$W/log/image.log" 2>&1 || { tail -20 "$W/log/image.log" >&2; exit 1; }
overlay "$W/out/$SUITE.cpio.zst" "$W/out/$SUITE-ct.cpio.zst" "$W/out/$SUITE-cases.txt"
mv "$W/out/$SUITE-ct.cpio.zst" "$W/out/$SUITE.cpio.zst"
