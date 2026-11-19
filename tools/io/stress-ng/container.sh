#!/bin/bash
# container.sh: build Debian trixie's stress-ng 0.19.02-1 as one static x86-64 binary inside an amd64 Debian container;
# testkit runs it (testkit/inputs.py:stress_ng) and writes provenance.json beside the outputs. /recipe is this folder
# (read-only), /work the output: bin/stress-ng, source.tar.xz (the orig tarball) and build.log.
#
# The build is tools/correctness/stress-remote.sh's `build` stage on remote.sh's `sysroot`, unchanged: Debian's gcc 14
# and libc unpacked (dpkg -x) into a sysroot from snapshot SNAP, the orig tarball plus Debian's packaging tarball and
# its patch series, `make STATIC=1 CC="x86_64-linux-gnu-gcc-14 --sysroot=$S"`. The sysroot also holds patch and
# xz-utils, which the container lacks (tools only, no headers or libraries the build links).
#
# Sources come through `apt-get source -d` (checked against the signed snapshot Sources) and then against
# sources.sha256; the orig tarball is the one the benchmark image fetches from
# https://deb.debian.org/debian/pool/main/s/stress-ng/stress-ng_0.19.02.orig.tar.xz (ffe89071...52bd).
# packages.txt names every sysroot package and version, debs.sha256 pins each archive. Without them (re-pinning) the
# closure of SYSROOT is resolved, packages.txt and debs.sha256 are written to /work, and the build stops: copy them here
# after checking them. The same holds for sources.sha256.
set -euo pipefail
SNAP=20260915T000000Z
MIRROR=http://snapshot.debian.org/archive/debian/$SNAP
SYSROOT='gcc-14 libc6-dev make bison m4 flex gawk zstd pkgconf patch xz-utils'
export DEBIAN_FRONTEND=noninteractive
KR=/usr/share/keyrings/debian-archive-keyring.gpg
[ -f "$KR" ] || KR=/usr/share/keyrings/debian-archive-keyring.pgp
# A private apt with an empty package database (remote.sh's): the downloaded closure does not depend on the image.
W=/build; S=$W/sysroot
mkdir -p "$W"/{apt/lists/partial,apt/cache/archives/partial,apt/etc/preferences.d,apt/etc/apt.conf.d,debs,src,log}
cat > "$W/apt/apt.conf" <<EOC
Dir::State "$W/apt"; Dir::State::Lists "$W/apt/lists"; Dir::State::status "$W/apt/status"; Dir::Cache "$W/apt/cache";
Dir::Etc::SourceList "$W/apt/sources.list"; Dir::Etc::SourceParts "$W/apt/etc/none";
Dir::Etc::Preferences "$W/apt/etc/preferences"; Dir::Etc::PreferencesParts "$W/apt/etc/preferences.d";
Dir::Etc::Parts "$W/apt/etc/apt.conf.d"; APT::Architecture "amd64"; APT::Architectures { "amd64"; };
APT::Architecture-Variants "amd64"; Debug::NoLocking "true"; APT::Install-Recommends "false"; Acquire::Check-Valid-Until "false"; Acquire::Retries "5";
EOC
touch "$W/apt/status"
printf 'deb [signed-by=%s] %s/ trixie main\ndeb-src [signed-by=%s] %s/ trixie main\n' "$KR" "$MIRROR" "$KR" "$MIRROR" > "$W/apt/sources.list"
export APT_CONFIG=$W/apt/apt.conf
apt-get update -qq

cd "$W/debs"
if [ -f /recipe/packages.txt ]; then apt-get download -qq $(cat /recipe/packages.txt)
else
    apt-get -y -qq -d install --no-install-recommends $SYSROOT >/dev/null
    cp "$W"/apt/cache/archives/*.deb .
    for d in *.deb; do echo "$(dpkg-deb -f "$d" Package)=$(dpkg-deb -f "$d" Version)"; done | sort > /work/packages.txt
fi
if [ ! -f /recipe/debs.sha256 ]; then sha256sum ./*.deb | sed 's| \./| |' > /work/debs.sha256; echo 'no debs.sha256 pins: wrote /work/debs.sha256' >&2; exit 1; fi
sha256sum -c --quiet /recipe/debs.sha256
[ "$(ls ./*.deb | wc -l)" = "$(wc -l < /recipe/debs.sha256)" ] || { echo 'archives differ from debs.sha256' >&2; exit 1; }
rm -rf "$S"; mkdir -p "$S"; for d in ./*.deb; do dpkg -x "$d" "$S"; done
for l in lib lib64 bin sbin; do [ -L "$S/$l" ] || { [ -d "$S/$l" ] && cp -a "$S/$l/." "$S/usr/$l/" && rm -rf "$S/$l"; ln -sfn usr/$l "$S/$l"; }; done
# Debian's non-libc shared libraries for the Debian tools run in place (the image's glibc runs them).
NL=$W/nativelibs; mkdir -p "$NL"
for f in "$S"/usr/lib/x86_64-linux-gnu/*.so*; do
    case ${f##*/} in libc.so*|libm.so*|libmvec*|libpthread*|libdl*|librt*|ld-*|libresolv*|libutil*|libanl*|libnss*|libthread_db*|libcrypt*|libBrokenLocale*|libmemusage*|libpcprofile*|libc_malloc*) ;;
    *) ln -sf "$f" "$NL/";; esac
done

cd "$W/src"
apt-get source -d -qq stress-ng=0.19.02-1
if [ ! -f /recipe/sources.sha256 ]; then sha256sum stress-ng_0.19.02* > /work/sources.sha256; echo 'no sources.sha256 pins: wrote /work/sources.sha256' >&2; exit 1; fi
sha256sum -c --quiet /recipe/sources.sha256

# stress-remote.sh build, with W=/build.
export PATH=$S/usr/bin:$PATH LD_LIBRARY_PATH=$NL
cd "$W"; rm -rf sng; mkdir sng; tar xJf src/stress-ng_0.19.02.orig.tar.xz -C sng --strip-components=1
tar xJf src/stress-ng_0.19.02-1.debian.tar.xz -C sng
cd sng; [ -f debian/patches/series ] && while read -r p; do case $p in ""|\#*) continue;; esac; patch -p1 < "debian/patches/$p"; done < debian/patches/series
make -j4 STATIC=1 CC="x86_64-linux-gnu-gcc-14 --sysroot=$S" > /work/build.log 2>&1 || { tail -40 /work/build.log >&2; exit 1; }
mkdir -p /work/bin; cp stress-ng /work/bin/stress-ng; cp "$W/src/stress-ng_0.19.02.orig.tar.xz" /work/source.tar.xz
/work/bin/stress-ng --version
