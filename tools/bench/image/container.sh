#!/bin/bash
# container.sh: build the benchmark initramfs inside an amd64 Debian container (Rosetta on a Mac), so any Mac can make
# it from this folder with no x86 machine: testkit runs this (testkit/artifacts.py:bench_image). The recipe itself is
# scripts/build.sh and scripts/mkroot.sh, natemu-raw/bench-20261117-01's from the reference box with Debian's archive at a
# fixed snapshot.debian.org date (build.sh's SNAP) instead of the moving mirror: they fetch pinned Debian trixie packages
# and sources, build the static benchmarks with Debian's gcc 14, run them once to record their expected outputs in init,
# and write out/bench.cpio. /recipe is this folder (read-only), /work the output. The container's own tools come from the
# same snapshot date.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
SNAP=$(sed -n 's/^SNAP=//p' /recipe/scripts/build.sh)
[ -n "$SNAP" ] || { echo 'no SNAP= line in scripts/build.sh' >&2; exit 1; }
KR=/usr/share/keyrings/debian-archive-keyring.gpg
[ -f "$KR" ] || KR=/usr/share/keyrings/debian-archive-keyring.pgp
rm -f /etc/apt/sources.list.d/* /etc/apt/sources.list
echo "deb [signed-by=$KR check-valid-until=no] http://snapshot.debian.org/archive/debian/$SNAP/ trixie main" > /etc/apt/sources.list
apt-get -o Acquire::Retries=5 update -qq
apt-get -o Acquire::Retries=5 install -y -qq --no-install-recommends curl ca-certificates python3 binutils tcl xz-utils >/dev/null
# Build inside the container's own file system: on the folder shared with the Mac even root cannot override file
# modes, and dpkg -x needs to (packages ship read-only folders that later packages write into).
bash /recipe/scripts/build.sh /build/b > /work/build.log 2>&1 || { tail -40 /work/build.log >&2; exit 1; }
cp /build/b/out/bench.cpio /build/b/out/expected.txt /work/
