#!/bin/bash
# container.sh: the Speedometer guest userland (bench cases speedometer-*, debug case speedometer-profile,
# tests/bench/run_speedometer.py): Debian 13's firefox-esr, Python 3 (the local web server), the DejaVu fonts and
# BusyBox, and WebKit's Speedometer 3.1 (WebKit/Speedometer at SP_COMMIT, release/3.1: its workloads are committed
# prebuilt, so nothing is built) at /sp, as one zstd-compressed newc cpio. testkit runs it in the amd64 Debian builder
# (testkit/inputs.py:speedometer_inputs); /recipe is this folder (read-only), /work the output: userland.cpio.zst,
# packages.txt and versions.txt.
#
# Speedometer is fetched at its commit with git, which is purged afterwards with ca-certificates. The Debian packages
# come from a fixed snapshot.debian.org date (SNAP, the bench image's); apt checks the archive's signature. packages.txt
# here pins the userland's installed packages and versions after the purge; a build whose list differs stops. Without
# packages.txt (re-pinning) the list is written to /work/packages.txt and the build stops: check it and copy it here.
#
# The results hook, the server, Firefox's profile and policies are not part of the userland: the runner appends them,
# with the guest's init and the mount points, as a second, uncompressed cpio (tests/bench/speedometer/). As
# tools/bench/glmark2/container.sh, the userland is the container's root without /proc, /sys, /dev, /tmp, the recipe
# and work folders and the files the container runtime provides; the archive is zero-padded to a multiple of 4 bytes.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
SNAP=20261120T000000Z
SP_COMMIT=1386415be8fef2f6b6bbdbe1828872471c5d802a   # WebKit/Speedometer release/3.1 (2025-03-11)
KR=/usr/share/keyrings/debian-archive-keyring.gpg
[ -f "$KR" ] || KR=/usr/share/keyrings/debian-archive-keyring.pgp
rm -f /etc/apt/sources.list.d/* /etc/apt/sources.list
echo "deb [signed-by=$KR check-valid-until=no] http://snapshot.debian.org/archive/debian/$SNAP/ trixie main" > /etc/apt/sources.list
apt-get -o Acquire::Retries=5 update -qq
apt-get -o Acquire::Retries=5 install -y -qq --no-install-recommends firefox-esr python3 fonts-dejavu-core \
  busybox-static zstd cpio >/dev/null
apt-mark manual firefox-esr python3 fonts-dejavu-core busybox-static zstd cpio >/dev/null
apt-get -o Acquire::Retries=5 install -y -qq --no-install-recommends git ca-certificates >/dev/null
git init -q /sp && cd /sp && git fetch -q --depth 1 https://github.com/WebKit/Speedometer "$SP_COMMIT" && git checkout -q FETCH_HEAD
test "$(git rev-parse HEAD)" = "$SP_COMMIT"
grep -q '<script src="resources/main.mjs" type="module"></script>' index.html   # where the runner's init puts the hook
{ echo "speedometer $(git rev-parse HEAD)"; echo "firefox $(firefox-esr --version 2>/dev/null)"; } > /work/versions.txt
rm -rf .git tests
cd / && apt-get purge -y -qq git ca-certificates >/dev/null
apt-get autoremove -y -qq --purge >/dev/null
dpkg-query -W -f='${Package} ${Version}\n' | LC_ALL=C sort > /build-packages.txt
if [ ! -f /recipe/packages.txt ]; then
  cp /build-packages.txt /work/packages.txt; echo 'no packages.txt pins: wrote /work/packages.txt' >&2; exit 1
fi
if ! diff <(LC_ALL=C sort /recipe/packages.txt) /build-packages.txt >&2; then
  echo 'the installed packages differ from packages.txt (above: < pinned, > installed)' >&2; exit 1
fi
cp /recipe/packages.txt /work/packages.txt
rm -rf /var/cache/apt/archives/*.deb /var/lib/apt/lists/* /build-packages.txt
find . -xdev \( -path ./proc -o -path ./sys -o -path ./dev -o -path ./tmp -o -path ./work -o -path ./recipe \
       -o -path ./etc/hosts -o -path ./etc/hostname -o -path ./etc/resolv.conf -o -path ./.dockerenv \) -prune -o -print \
  | LC_ALL=C sort | cpio -o -H newc --quiet | zstd -q -T0 -10 -o /work/userland.cpio.zst
truncate -s %4 /work/userland.cpio.zst
