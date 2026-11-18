#!/bin/bash
# container.sh: build the network-tools root (iperf3, curl, openssl and the libraries they load) inside an amd64 Debian
# container; testkit runs it (testkit/inputs.py). /recipe is this folder (read-only), /work the output: rootfs.tar,
# packages.txt and debs.sha256. The runner tools/run_net_linux.py copies this tree into its guest root.
#
# packages.txt names every package and version; debs.sha256 pins each archive. Packages come from the snapshot.debian.org
# archive at SNAP (debian and debian-security), so the versions stay downloadable; apt checks the archive's signature
# and the pins check each file. The root is the packages unpacked (dpkg -x, no maintainer scripts) plus the lib and lib64
# links a merged-/usr system has. Without debs.sha256 (re-pinning) the archives' hashes are written to /work/debs.sha256
# and the build stops: copy that file here after checking it.
set -euo pipefail
SNAP=20261108T000000Z
export DEBIAN_FRONTEND=noninteractive
KR=/usr/share/keyrings/debian-archive-keyring.gpg
[ -f "$KR" ] || KR=/usr/share/keyrings/debian-archive-keyring.pgp
rm -f /etc/apt/sources.list.d/* /etc/apt/sources.list
cat > /etc/apt/sources.list <<EOF
deb [signed-by=$KR check-valid-until=no] http://snapshot.debian.org/archive/debian/$SNAP/ trixie main
deb [signed-by=$KR check-valid-until=no] http://snapshot.debian.org/archive/debian/$SNAP/ trixie-updates main
deb [signed-by=$KR check-valid-until=no] http://snapshot.debian.org/archive/debian-security/$SNAP/ trixie-security main
EOF
apt-get -o Acquire::Retries=5 update -qq
mkdir -p /build/debs && cd /build/debs
apt-get -o Acquire::Retries=5 download -qq $(cat /recipe/packages.txt)
if [ ! -f /recipe/debs.sha256 ]; then sha256sum ./*.deb | sed 's| \./| |' > /work/debs.sha256; echo 'no debs.sha256 pins: wrote /work/debs.sha256' >&2; exit 1; fi
sha256sum -c --quiet /recipe/debs.sha256
[ "$(ls ./*.deb | wc -l)" = "$(wc -l < /recipe/debs.sha256)" ] || { echo 'archives differ from debs.sha256' >&2; exit 1; }
R=/build/rootfs; mkdir -p $R
for d in ./*.deb; do dpkg -x "$d" $R; done
for l in lib lib64; do [ -e $R/$l ] || ln -s usr/$l $R/$l; done
tar -C $R --numeric-owner -cf /work/rootfs.tar .
cp /recipe/packages.txt /recipe/debs.sha256 /work/
