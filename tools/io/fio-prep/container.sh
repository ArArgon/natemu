#!/bin/bash
# container.sh: download Debian's fio 3.39-1 and its Depends/Pre-Depends closure inside an amd64 Debian container;
# testkit runs it (testkit/inputs.py:fio_prep), then closure.py on the Mac unpacks the archives and writes the folder
# tools/run_io_matrix.py and tools/run_fio_admission.py read as --fio-prep. /recipe is this folder (read-only), /work
# the output: debs/ (the archives as apt names them) and Packages.trixie.xz (the index the runners pin).
#
# packages.txt names every package and version (natemu-raw/m2-fio-setup-20261114-01's closure, in its order);
# debs.sha256 pins each archive. Packages come from snapshot.debian.org's trixie main at SNAP, the index
# run_fio_admission.INDEX_SHA pins: apt checks the archive's signature, the index is fetched against the signed
# InRelease's hash, and the pins check each file. Without debs.sha256 (re-pinning) the archives' hashes are written to
# /work/debs.sha256 and the build stops: copy that file here after checking it.
set -euo pipefail
SNAP=20261108T000000Z
MIRROR=http://snapshot.debian.org/archive/debian/$SNAP
export DEBIAN_FRONTEND=noninteractive
KR=/usr/share/keyrings/debian-archive-keyring.gpg
[ -f "$KR" ] || KR=/usr/share/keyrings/debian-archive-keyring.pgp
rm -f /etc/apt/sources.list.d/* /etc/apt/sources.list
echo "deb [signed-by=$KR check-valid-until=no] $MIRROR/ trixie main" > /etc/apt/sources.list
apt-get -o Acquire::Retries=5 update -qq
mkdir -p /build/debs
# The index itself, checked against the hash the signed InRelease gives for it.
REL=$(ls /var/lib/apt/lists/*_dists_trixie_InRelease)
SHA=$(awk '/^SHA256:/ {s = 1; next} /^[^ ]/ {s = 0} s && $3 == "main/binary-amd64/Packages.xz" {print $1}' "$REL")
[ -n "$SHA" ] || { echo "no Packages.xz hash in $REL" >&2; exit 1; }
/usr/lib/apt/apt-helper -o Acquire::Retries=5 download-file "$MIRROR/dists/trixie/main/binary-amd64/Packages.xz" \
    /build/Packages.trixie.xz "SHA256:$SHA"
cd /build/debs
apt-get -o Acquire::Retries=5 download -qq $(cat /recipe/packages.txt)
if [ ! -f /recipe/debs.sha256 ]; then sha256sum ./*.deb | sed 's| \./| |' > /work/debs.sha256; echo 'no debs.sha256 pins: wrote /work/debs.sha256' >&2; exit 1; fi
sha256sum -c --quiet /recipe/debs.sha256
[ "$(ls ./*.deb | wc -l)" = "$(wc -l < /recipe/debs.sha256)" ] || { echo 'archives differ from debs.sha256' >&2; exit 1; }
mkdir -p /work/debs && cp ./*.deb /work/debs/ && cp /build/Packages.trixie.xz /work/
