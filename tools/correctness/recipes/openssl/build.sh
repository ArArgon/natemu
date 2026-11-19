#!/bin/bash
# build.sh: the OpenSSL campaign image (testkit/inputs.py:campaign_image('openssl'); common.sh says how it runs).
# remote.sh's stages sysroot, openssl, play and image openssl, with the source remote.sh expects in src/ and no script
# fetched: Debian's openssl 3.5.7-1~deb13u2 through apt-get source -d (the .dsc checked against the signed Sources,
# the orig and Debian tarballs against the .dsc), all three pinned in sources.sha256. extra-debs.sha256 pins perl and
# the libraries it adds to play's package set (OpenSSL's test harness, test/run_tests.pl, runs in the guest).
#
# openssl-list.txt: `PART TEST` per run, TEST = MASK:RECIPE as run-openssl.sh's CT-RESULT lines name it and PART the
# kernel's part= word. run-openssl.sh K/N runs entries K, K+N, ... of masks x recipes (masks outer), so the list is
# made by the same loops over the same two files the image's /ct holds.
set -euo pipefail
. /recipe/correctness/recipes/common.sh
SUITE=openssl
PARTS=2                                               # tests/correctness/family.toml: parts = 2
if [ "${1:-}" != user ]; then root_phase "$(realpath "$0")" "$SUITE"; exit 0; fi

user_env
sysroot
sources 'openssl=3.5.7-1~deb13u2' openssl/sources.sha256
play
extra_debs openssl/extra-debs.sha256 perl
stop_if_repin

stage openssl > "$W/log/openssl-stage.log" 2>&1 || { tail -20 "$W/log/openssl-stage.log" "$W/log/openssl-make.log" >&2; exit 1; }
i=0
while read -r m cap; do
    while read -r t; do echo "$((i % PARTS))/$PARTS $m:$t"; i=$((i + 1)); done < "$T/openssl-tests.txt"
done < "$T/openssl-masks.txt" > "$W/out/$SUITE-list.txt"
say "$(wc -l < "$W/out/$SUITE-list.txt") runs listed"
stage image "$SUITE" > "$W/log/image.log" 2>&1 || { tail -20 "$W/log/image.log" >&2; exit 1; }
