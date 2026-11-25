#!/bin/bash
# container.sh: the Web Tooling Benchmark's guest userland (debug case web-tooling-profile,
# tests/bench/run_web_tooling.py): Debian 13's Node.js and BusyBox, and the V8 team's Web Tooling Benchmark v0.5.3
# (v8/web-tooling-benchmark at WTB_COMMIT) built to its single-file bundle /wtb/cli.js, as one zstd-compressed newc cpio.
# testkit runs it in the amd64 Debian builder (testkit/inputs.py:web_tooling_inputs); /recipe is this folder
# (read-only), /work the output: userland.cpio.zst, packages.txt and versions.txt.
#
# The bundle is built in the same container with Debian's npm: `npm ci` installs the benchmark's dependencies at the
# versions and hashes of its committed package-lock.json, and its build step writes dist/cli.js. Then npm, git and
# ca-certificates are purged, so the userland holds only what the guest runs. The Debian packages come from a fixed
# snapshot.debian.org date (SNAP, the bench image's); apt checks the archive's signature. packages.txt here pins the
# userland's installed packages and versions after the purge; a build whose list differs stops. Without packages.txt
# (re-pinning) the list is written to /work/packages.txt and the build stops: check it and copy it here. versions.txt
# records the commit, Node's and npm's versions and cli.js's sha256 (recorded, never required).
#
# As tools/bench/glmark2/container.sh, the userland is the container's root without /proc, /sys, /dev, /tmp, the
# recipe and work folders and the files the container runtime provides; the runner appends the guest's init, its
# helpers and the mount points as a second, uncompressed cpio. The archive is zero-padded to a multiple of 4 bytes.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
SNAP=20261120T000000Z
WTB_COMMIT=4a12828c6a1eed02a70c011bd080445dd319a05f   # v8/web-tooling-benchmark, "Update dependencies (#64)", v0.5.3
KR=/usr/share/keyrings/debian-archive-keyring.gpg
[ -f "$KR" ] || KR=/usr/share/keyrings/debian-archive-keyring.pgp
rm -f /etc/apt/sources.list.d/* /etc/apt/sources.list
echo "deb [signed-by=$KR check-valid-until=no] http://snapshot.debian.org/archive/debian/$SNAP/ trixie main" > /etc/apt/sources.list
apt-get -o Acquire::Retries=5 update -qq
apt-get -o Acquire::Retries=5 install -y -qq --no-install-recommends nodejs busybox-static zstd cpio >/dev/null
apt-mark manual nodejs busybox-static zstd cpio >/dev/null
apt-get -o Acquire::Retries=5 install -y -qq --no-install-recommends npm git ca-certificates >/dev/null
git clone -q https://github.com/v8/web-tooling-benchmark /build
cd /build && git checkout -q "$WTB_COMMIT" && test "$(git rev-parse HEAD)" = "$WTB_COMMIT"
npm ci --no-audit --no-fund >/build/npm.log 2>&1 || { tail -40 /build/npm.log >&2; exit 1; }
test -s dist/cli.js || { echo 'npm ci wrote no dist/cli.js' >&2; tail -40 /build/npm.log >&2; exit 1; }
mkdir -p /wtb && cp dist/cli.js /wtb/cli.js
{ echo "wtb $(git rev-parse HEAD)"; echo "node $(node --version)"; echo "npm $(npm --version)"
  echo "cli.js $(sha256sum /wtb/cli.js | cut -d' ' -f1)"; } > /work/versions.txt
cd / && rm -rf /build /root/.npm /root/.cache /root/.config
apt-get purge -y -qq npm git ca-certificates >/dev/null
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
