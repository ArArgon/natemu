#!/bin/bash
# kms_pattern.sh: build tests/devices/kms_pattern.c as one static x86-64 binary in the amd64 Debian builder;
# testkit/inputs.py kms_pattern() runs it with /recipe holding kms_pattern.c, this script, and the sysroot pins it
# shares with the stress-ng recipe (tools/io/stress-ng/packages.txt and debs.sha256: Debian trixie's gcc 14, glibc
# 2.41 and linux-libc-dev 6.12.107, the guest kernel's own UAPI headers, from snapshot SNAP). /work gets kms_pattern
# and build.log.
#
# The sysroot is tools/io/stress-ng/container.sh's: a private apt with an empty package database downloads exactly
# the pinned archives, checked against debs.sha256, and dpkg -x unpacks them into $S; the compiler runs from there.
set -euo pipefail
SNAP=20260915T000000Z
MIRROR=http://snapshot.debian.org/archive/debian/$SNAP
export DEBIAN_FRONTEND=noninteractive
KR=/usr/share/keyrings/debian-archive-keyring.gpg
[ -f "$KR" ] || KR=/usr/share/keyrings/debian-archive-keyring.pgp
W=/build; S=$W/sysroot
mkdir -p "$W"/{apt/lists/partial,apt/cache/archives/partial,apt/etc/preferences.d,apt/etc/apt.conf.d,debs}
cat > "$W/apt/apt.conf" <<EOC
Dir::State "$W/apt"; Dir::State::Lists "$W/apt/lists"; Dir::State::status "$W/apt/status"; Dir::Cache "$W/apt/cache";
Dir::Etc::SourceList "$W/apt/sources.list"; Dir::Etc::SourceParts "$W/apt/etc/none";
Dir::Etc::Preferences "$W/apt/etc/preferences"; Dir::Etc::PreferencesParts "$W/apt/etc/preferences.d";
Dir::Etc::Parts "$W/apt/etc/apt.conf.d"; APT::Architecture "amd64"; APT::Architectures { "amd64"; };
APT::Architecture-Variants "amd64"; Debug::NoLocking "true"; APT::Install-Recommends "false"; Acquire::Check-Valid-Until "false"; Acquire::Retries "5";
EOC
touch "$W/apt/status"
printf 'deb [signed-by=%s] %s/ trixie main\n' "$KR" "$MIRROR" > "$W/apt/sources.list"
export APT_CONFIG=$W/apt/apt.conf
apt-get update -qq
cd "$W/debs"
apt-get download -qq $(cat /recipe/packages.txt)
sha256sum -c --quiet /recipe/debs.sha256
[ "$(ls ./*.deb | wc -l)" = "$(wc -l < /recipe/debs.sha256)" ] || { echo 'archives differ from debs.sha256' >&2; exit 1; }
rm -rf "$S"; mkdir -p "$S"; for d in ./*.deb; do dpkg -x "$d" "$S"; done
for l in lib lib64 bin sbin; do [ -L "$S/$l" ] || { [ -d "$S/$l" ] && cp -a "$S/$l/." "$S/usr/$l/" && rm -rf "$S/$l"; ln -sfn usr/$l "$S/$l"; }; done
# The sysroot's non-libc shared libraries for its tools, run by the image's glibc (stress-ng's recipe does the same).
NL=$W/nativelibs; mkdir -p "$NL"
for f in "$S"/usr/lib/x86_64-linux-gnu/*.so*; do
    case ${f##*/} in libc.so*|libm.so*|libmvec*|libpthread*|libdl*|librt*|ld-*|libresolv*|libutil*|libanl*|libnss*|libthread_db*|libcrypt*|libBrokenLocale*|libmemusage*|libpcprofile*|libc_malloc*) ;;
    *) ln -sf "$f" "$NL/";; esac
done
export PATH=$S/usr/bin:$PATH LD_LIBRARY_PATH=$NL
# Fixed target flags (no -march=native): the build does not depend on the machine it runs on.
x86_64-linux-gnu-gcc-14 --sysroot="$S" -static -O2 -march=x86-64 -Wall -Wextra -Werror -o /work/kms_pattern \
    /recipe/kms_pattern.c > /work/build.log 2>&1 || { cat /work/build.log >&2; exit 1; }
x86_64-linux-gnu-gcc-14 --version | head -1 >> /work/build.log
