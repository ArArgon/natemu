#!/bin/bash
# container.sh: kvm-unit-tests for x86_64 (x86/dummy.flat and x86/smptest.flat), built in the amd64 Debian builder
# (testkit/artifacts.py BENCH_BUILDER) by tests/boot/built_inputs.py:kvm_unit_tests, on this Mac through Rosetta or
# natively on the x86 builder. /recipe is this folder (read-only); /work holds the source on entry (src.tar.gz: the
# pinned download kvm-unit-tests-src, upstream commit a2891d9528eff31188bb16f08980637532735d1b) and the outputs on exit:
# dummy.flat, smptest.flat (Multiboot v1 images, ELF32), build.log and built.sha256 (what was built: recorded, never
# required).
#
# The toolchain is Debian trixie's gcc 14 and binutils from snapshot.debian.org at SNAP, installed as root into this
# throwaway container (as tools/correctness/recipes/common.sh installs its tools): apt checks the archive's signature
# against the image's keyring and every archive is checked against debs.sha256. Without debs.sha256 (re-pinning) the
# archives' hashes go to /work/debs.sha256 and the build stops: check them and copy the file here.
#
# The build is upstream's own: ./configure --arch=x86_64 in the source tree, then make for the two images. -m16 and
# ld -m elf_i386 (realmode.flat) are not needed for them; the B0 spike showed both work in this container.
set -euo pipefail
SNAP=20260915T000000Z
MIRROR=http://snapshot.debian.org/archive/debian/$SNAP
PKGS='gcc make'
TESTS='dummy smptest'
export DEBIAN_FRONTEND=noninteractive LC_ALL=C TZ=UTC
KR=/usr/share/keyrings/debian-archive-keyring.gpg
[ -f "$KR" ] || KR=/usr/share/keyrings/debian-archive-keyring.pgp
rm -f /etc/apt/sources.list.d/* /etc/apt/sources.list
echo "deb [signed-by=$KR check-valid-until=no] $MIRROR/ trixie main" > /etc/apt/sources.list
apt-get -o Acquire::Retries=5 update -qq
rm -f /var/cache/apt/archives/*.deb
apt-get -o Acquire::Retries=5 -y -qq -d install --no-install-recommends $PKGS >/dev/null
(cd /var/cache/apt/archives && sha256sum -- *.deb) | sort -k2 > /tmp/debs.lst
if [ ! -f /recipe/debs.sha256 ]; then cp /tmp/debs.lst /work/debs.sha256; echo 'no debs.sha256 pins: wrote /work/debs.sha256' >&2; exit 1; fi
diff <(sort -k2 /recipe/debs.sha256) /tmp/debs.lst >&2 || { echo 'archives differ from debs.sha256 (< pinned, > downloaded)' >&2; exit 1; }
apt-get -y -qq install --no-install-recommends $PKGS >/dev/null

mkdir -p /build && cd /build
tar xzf /work/src.tar.gz --strip-components=1
{ ./configure --arch=x86_64 && make -j4 $(for t in $TESTS; do echo "x86/$t.flat"; done); } > /work/build.log 2>&1 \
    || { tail -40 /work/build.log >&2; exit 1; }
for t in $TESTS; do cp "x86/$t.flat" "/work/$t.flat"; done
(cd /work && sha256sum -- *.flat) > /work/built.sha256
gcc --version | head -1 >> /work/build.log
cat /work/built.sha256
