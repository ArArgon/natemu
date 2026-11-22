#!/bin/bash
# build.sh: the LTP campaign image for 32-bit code (testkit/inputs.py:campaign_image('ltp32'); common.sh says how it
# runs): recipes/ltp's LTP (the same pinned ltp-full-20250530, sources.sha256 and tools/correctness/ltp.sha256), built
# for i686 by the sysroot's gcc 14 -m32 -march=i686 (i386.sh's multilib, multilib-debs.sha256; Debian's i386 default)
# with remote.sh's ltp stage's configure options, as a cross build (--host i686: configure runs nothing it built, so
# the build is the same on a builder that cannot run 32-bit code). Dynamic, as the 64-bit build: the image carries the
# multilib's 32-bit libc and libgcc_s (libc6-i386, lib32gcc-s1: /usr/lib32, /lib/ld-linux.so.2) beside the 64-bit root.
# The selection is the 64-bit suite's: ltp-list.py over the same runtest files and sources (1,446 entries, 256
# excluded; another count stops the build). Programs not built for i686 are listed in log/ltp32-not-built.txt; the
# guest reports their entries with exit 127. Every LTP system call test thus goes through the kernel's 32-bit entry
# (INT 0x80 and the vDSO's SYSENTER), its compat layer and 32-bit signal frames; the `_16` variants use the 16-bit
# uid system calls, which only 32-bit code has.
#
# The guest's runner is run-ltp.sh (the suite's ct word is ltp), with /boot/config-* for LTP's kernel config checks,
# as remote.sh's image stage gives the 64-bit image. ltp32-cases.txt: recipes/ltp/cases.sh's split into PARTS parts.
set -euo pipefail
. /recipe/correctness/recipes/common.sh
. /recipe/correctness/i386.sh
SUITE=ltp32
PARTS=4                                               # tests/correctness/family.toml: parts = 4
LTP=ltp-full-20250530
URL=https://github.com/linux-test-project/ltp/releases/download/20250530/$LTP.tar.xz
if [ "${1:-}" != user ]; then root_phase "$(realpath "$0")" "$SUITE"; exit 0; fi

user_env
sysroot
multilib ltp32/multilib-debs.sha256
say "curl $URL"
mkdir -p "$W/src"
curl -sSLf --retry 5 -o "$W/src/$LTP.tar.xz" "$URL" || { echo "Failed to fetch $URL" >&2; exit 1; }
(cd "$W/src" && sha256sum -- "$LTP.tar.xz") > "$H/src.lst"; pins "$H/src.lst" ltp32/sources.sha256
echo "$(cat "$T/ltp.sha256")  $W/src/$LTP.tar.xz" | sha256sum -c --quiet
echo "$URL $(sha256sum < "$W/src/$LTP.tar.xz" | cut -d' ' -f1) $LTP.tar.xz" >> "$W/out/downloads.txt"
play
stop_if_repin

say "building LTP for i686"
S=$W/sysroot
export PATH=$S/usr/bin:$PATH LD_LIBRARY_PATH=$W/nativelibs M4=$S/usr/bin/m4 BISON_PKGDATADIR=$S/usr/share/bison
cd "$W"; rm -rf ltp-src ltp-inst; tar xJf "src/$LTP.tar.xz"; mv "$LTP" ltp-src; cd ltp-src
rm testcases/kernel/syscalls/listmount/listmount04.c   # as remote.sh's ltp stage: headers newer than trixie's
B=x86_64-linux-gnu
./configure --build=x86_64-pc-linux-gnu --host=i686-pc-linux-gnu --prefix=/opt/ltp --without-modules --without-numa \
    --without-tirpc CC="$CC32 -march=i686" HOSTCC="$S/usr/bin/$B-gcc-14 --sysroot=$S" AR=$B-ar RANLIB=$B-ranlib \
    STRIP=$B-strip OBJCOPY=$B-objcopy PKG_CONFIG_LIBDIR=/nonexistent CFLAGS="-O2 -g0" > "$W/log/ltp-configure.log" 2>&1 ||
    { tail -30 "$W/log/ltp-configure.log" >&2; exit 1; }
make -k -j4 > "$W/log/ltp-make.log" 2>&1 || true
make -k install DESTDIR="$W/ltp-inst" > "$W/log/ltp-install.log" 2>&1 || true
cd "$W"; rm -rf ltp-src
L=$W/ltp-inst/opt/ltp
[ "$(od -An -tx1 -j18 -N1 "$L/testcases/bin/getpid01")" = " 03" ] || { echo "getpid01 is not an i386 program" >&2; exit 1; }

rm -rf "$H/ltp-list-src"; mkdir "$H/ltp-list-src"       # ltp-list.py reads the sources' testcases, as recipes/ltp
tar xJf "$W/src/$LTP.tar.xz" -C "$H/ltp-list-src" --strip-components=1 "$LTP/testcases"
python3 -I "$T/ltp-list.py" "$L" "$H/ltp-list-src" "$W/out" | tee "$W/log/ltp-list.log" >&2
rm -rf "$H/ltp-list-src"
cp "$W/out/ltp-list.txt" "$W/out/ltp-excluded.txt" "$W/out/ltp-keep.txt" "$W/log/"
listed=$(wc -l < "$W/out/ltp-list.txt"); excluded=$(wc -l < "$W/out/ltp-excluded.txt")
[ "$listed" = 1446 ] && [ "$excluded" = 256 ] || {
    echo "ltp-list.py listed $listed entries and excluded $excluded: the 64-bit suite's selection is 1,446 and 256" >&2; exit 1; }
# The entries whose program was not built (the first word of the command's last `;` part, as recipes/ltp).
awk -F'\t' '{n = split($3, s, ";"); split(s[n], c, " "); print c[1]}' "$W/out/ltp-list.txt" | sort -u |
    while read -r p; do [ -e "$L/testcases/bin/$p" ] || echo "$p"; done > "$W/log/ltp32-not-built.txt"
say "programs not built: $(tr '\n' ' ' < "$W/log/ltp32-not-built.txt")"
sh "$RC/ltp/cases.sh" "$W/out/ltp-list.txt" "$PARTS" > "$W/out/$SUITE-cases.txt"
say "$(wc -l < "$W/out/$SUITE-cases.txt") entries listed"

say "the image"
P=$W/payload; rm -rf "$P"; mkdir -p "$P/opt/ltp/testcases/bin" "$P/ct" "$P/boot" "$P/usr/lib32" "$P/usr/lib"
(cd "$L" && tar cf - --exclude=./testcases/bin .) | tar xf - -C "$P/opt/ltp"     # remote.sh's image stage for ltp
(cd "$L/testcases/bin" && tar cf - $(cat "$W/out/ltp-keep.txt")) | tar xf - -C "$P/opt/ltp/testcases/bin"
(cd "$P/opt/ltp/testcases/bin" && for f in *; do [ "$(head -c4 "$f")" = "$(printf '\177ELF')" ] && $B-strip "$f"; done; true)
cp "$W/out/ltp-list.txt" "$P/ct/"
# The 32-bit libc the programs were linked with (they need only libc.so.6 and libm.so.6) and libgcc_s (pthread_cancel)
cp -a "$S"/usr/lib32/{ld-linux.so.2,libc.so.6,libm.so.6,libgcc_s.so.1} "$P/usr/lib32/"
ln -sfn ../lib32/ld-linux.so.2 "$P/usr/lib/ld-linux.so.2"; ln -sfn usr/lib32 "$P/lib32"
curl -sSLf --retry 5 -o "$H/k.deb" "$MIRROR/pool/main/l/linux-signed-amd64/$K"   # the kernel's config (play() pins it)
echo "$KDEB_SHA  $H/k.deb" | sha256sum -c --quiet
rm -rf "$H/kx"; dpkg -x "$H/k.deb" "$H/kx"; cp "$H"/kx/boot/config-* "$P/boot/"; rm -rf "$H/kx" "$H/k.deb"
image32 "$SUITE" ltp > "$W/log/image.log" 2>&1 || { tail -20 "$W/log/image.log" >&2; exit 1; }
