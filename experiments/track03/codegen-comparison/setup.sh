#!/bin/sh
set -eu
export DEBIAN_FRONTEND=noninteractive
cat /etc/os-release
uname -a
mkdir -p /work/packages/partial
apt-get update
packages='clang llvm cmake ninja-build meson nasm python3 python3-venv python3-pip build-essential git ca-certificates pkg-config libglib2.0-dev zlib1g-dev libpixman-1-dev libjemalloc-dev libxxhash-dev libfmt-dev libvixl-dev libzstd-dev libslirp-dev'
apt-get --print-uris --yes --no-install-recommends install $packages > /work/package-uris.txt
# Leave transfer allowance for indexes and the three source/dependency trees.
awk 'substr($1,1,1)==sprintf("%c",39) { n++; if ($3 !~ /^[0-9]+$/) bad=1; total += $3 } END { print "package_bytes=" total; if (!n || bad || total > 700*1024*1024) exit 1 }' /work/package-uris.txt
apt-get -o Dir::Cache::archives=/work/packages --download-only --yes --no-install-recommends install $packages
# Install only the retained packages; no second network dependency resolution.
apt-get -o Dir::Cache::archives=/work/packages --no-download --yes --no-install-recommends install $packages
dpkg-query -W > /work/package-versions.txt
clang --version
cmake --version
ninja --version
meson --version
nasm --version
python3 --version
