#!/bin/bash
# container.sh: OVMF for x86-64 (edk2's OvmfPkg/OvmfPkgX64.dsc, DEBUG, GCC toolchain), without SMM and without Secure
# Boot, built in the amd64 Debian builder (testkit/artifacts.py BENCH_BUILDER) by tests/boot/built_inputs.py:ovmf, on
# this Mac through Rosetta or natively on the x86 builder. /recipe is this folder (read-only); /work holds the sources
# on entry, the pinned downloads of tests/resources.toml:
#   edk2.tar.gz      edk2-stable202608 (commit 2970e5699ba6267f3384ffab20f96647578aebc8)
#   openssl.tar.gz   its submodule CryptoPkg/Library/OpensslLib/openssl at 8cf17aaeb4599f8af87fefd810b5b5fee90fe69e
#   brotli.tar.gz    its submodules MdeModulePkg/Library/BrotliCustomDecompressLib/brotli and
#                    BaseTools/Source/C/BrotliCompress/brotli at e230f474b87134e8c6c85b630084c612057f253e
#   mipisyst.tar.gz  MdePkg/Library/MipiSysTLib/mipisyst at 370b5944c046bab043dd8b133727b2135af7747a
#   mbedtls.tar.gz   CryptoPkg/Library/MbedTlsLib/mbedtls at 0bebf8b8c7f07abe3571ded48a11aa907a1ffb20
#   libspdm.tar.gz   SecurityPkg/DeviceSecurity/SpdmLib/libspdm at f55cf6d48ec69b4ac60a63903e9c6a2cb0fd155d
# (the last three only for the include folders their packages declare; the other submodules are not used by this
# build), and on exit the outputs: OVMF.fd (the 4 MiB image for FFC00000H-FFFFFFFFH), OVMF_CODE.fd, OVMF_VARS.fd,
# SECFV.Fv.map (each SECFV module's flash address and entry point), ResetVector.bin, report.txt (the build's PCD and
# flash report: every fixed address), build.log and built.sha256 (what was built: recorded, never required).
#
# The toolchain is Debian trixie's (gcc 14, nasm, iasl, uuid-dev, python3) from snapshot.debian.org at SNAP, installed
# as root into this throwaway container (as tools/correctness/recipes/common.sh installs its tools): apt checks the
# archive's signature against the image's keyring and every archive is checked against debs.sha256. Without it
# (re-pinning) the archives' hashes go to /work/debs.sha256 and the build stops: check them and copy the file here.
#
# The build is upstream's: make -C BaseTools, edksetup.sh, then `build` with the DSC's defaults except SMM_REQUIRE and
# SECURE_BOOT_ENABLE given explicitly (both FALSE, their defaults too).
set -euo pipefail
SNAP=20260915T000000Z
MIRROR=http://snapshot.debian.org/archive/debian/$SNAP
PKGS='gcc g++ make nasm acpica-tools uuid-dev python3'
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

E=/build/edk2
mkdir -p $E && cd $E
tar xzf /work/edk2.tar.gz --strip-components=1
sub() { mkdir -p "$2" && tar xzf "/work/$1.tar.gz" -C "$2" --strip-components=1; }
sub openssl CryptoPkg/Library/OpensslLib/openssl
sub brotli MdeModulePkg/Library/BrotliCustomDecompressLib/brotli
sub brotli BaseTools/Source/C/BrotliCompress/brotli
sub mipisyst MdePkg/Library/MipiSysTLib/mipisyst
sub mbedtls CryptoPkg/Library/MbedTlsLib/mbedtls
sub libspdm SecurityPkg/DeviceSecurity/SpdmLib/libspdm
export WORKSPACE=$E PYTHON_COMMAND=python3
log=/work/build.log
{ make -C BaseTools -j4 && set +u && . ./edksetup.sh && set -u &&
  build -a X64 -t GCC -b DEBUG -n 4 -p OvmfPkg/OvmfPkgX64.dsc -D SMM_REQUIRE=FALSE -D SECURE_BOOT_ENABLE=FALSE \
        -y /work/report.txt -Y PCD -Y FLASH; } > $log 2>&1 \
    || { tail -60 $log >&2; exit 1; }
B=$E/Build/OvmfX64/DEBUG_GCC
for f in OVMF.fd OVMF_CODE.fd OVMF_VARS.fd SECFV.Fv.map; do cp "$B/FV/$f" /work/; done
cp "$B/X64/OvmfPkg/ResetVector/ResetVector/OUTPUT/ResetVector.bin" /work/
{ gcc --version | head -1; nasm -v; iasl -v 2>&1 | grep -m1 -i version; python3 --version; } >> $log
(cd /work && sha256sum -- OVMF.fd OVMF_CODE.fd OVMF_VARS.fd SECFV.Fv.map ResetVector.bin report.txt) > /work/built.sha256
cat /work/built.sha256
