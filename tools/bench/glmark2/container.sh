#!/bin/bash
# container.sh: the glmark2 benchmark's guest userland (bench cases glmark2-*, tests/bench/run_glmark2.py): Debian 13's
# glmark2 2023.01 (X11), Mesa's llvmpipe (software OpenGL), Xvfb and BusyBox, as one zstd-compressed newc cpio. testkit
# runs it in the amd64 Debian builder (testkit/inputs.py:glmark2_inputs); /recipe is this folder (read-only), /work the
# output: userland.cpio.zst and packages.txt.
#
# The packages come from Debian's archive at a fixed snapshot.debian.org date (SNAP, the bench image's), so they stay
# downloadable; apt checks the archive's signature. packages.txt here pins the result: every package of the userland and
# its version (the builder image's own and what apt adds); a build whose list differs stops. Without packages.txt
# (re-pinning) the list is written to /work/packages.txt and the build stops: check it and copy it here.
#
# The userland is the container's root after the install (the builder image is pinned by digest), without /proc, /sys,
# /dev, /tmp, the recipe and work folders and the files the container runtime provides (/etc/hosts, /etc/hostname,
# /etc/resolv.conf, /.dockerenv): the runner adds the guest's init, /dev/console and those folders as a second,
# uncompressed cpio. The archive is zero-padded to a multiple of 4 bytes, as the kernel needs before an appended cpio.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
SNAP=20261120T000000Z
KR=/usr/share/keyrings/debian-archive-keyring.gpg
[ -f "$KR" ] || KR=/usr/share/keyrings/debian-archive-keyring.pgp
rm -f /etc/apt/sources.list.d/* /etc/apt/sources.list
echo "deb [signed-by=$KR check-valid-until=no] http://snapshot.debian.org/archive/debian/$SNAP/ trixie main" > /etc/apt/sources.list
apt-get -o Acquire::Retries=5 update -qq
apt-get -o Acquire::Retries=5 install -y -qq --no-install-recommends xvfb xauth glmark2-x11 libgl1-mesa-dri mesa-utils \
  busybox-static zstd cpio >/dev/null
dpkg-query -W -f='${Package} ${Version}\n' | LC_ALL=C sort > /build-packages.txt
if [ ! -f /recipe/packages.txt ]; then
  cp /build-packages.txt /work/packages.txt; echo 'no packages.txt pins: wrote /work/packages.txt' >&2; exit 1
fi
if ! diff <(LC_ALL=C sort /recipe/packages.txt) /build-packages.txt >&2; then
  echo 'the installed packages differ from packages.txt (above: < pinned, > installed)' >&2; exit 1
fi
cp /recipe/packages.txt /work/packages.txt
rm -rf /var/cache/apt/archives/*.deb /var/lib/apt/lists/* /build-packages.txt
cd /
find . -xdev \( -path ./proc -o -path ./sys -o -path ./dev -o -path ./tmp -o -path ./work -o -path ./recipe \
       -o -path ./etc/hosts -o -path ./etc/hostname -o -path ./etc/resolv.conf -o -path ./.dockerenv \) -prune -o -print \
  | LC_ALL=C sort | cpio -o -H newc --quiet | zstd -q -T0 -10 -o /work/userland.cpio.zst
truncate -s %4 /work/userland.cpio.zst
