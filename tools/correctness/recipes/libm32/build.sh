#!/bin/bash
# build.sh: the glibc math campaign image for 32-bit code (testkit/inputs.py:campaign_image('libm32'); common.sh says
# how it runs): recipes/libm's glibc (Debian's 2.41-12+deb13u4 source and patch series, sources.sha256), built for
# i686 by the sysroot's gcc 14 -m32 -march=i686 (i386.sh's multilib, multilib-debs.sha256; Debian's i386 baseline) as
# a cross build (--host i686-linux-gnu), with remote.sh's glibc stage's configure options, then
# `make check subdirs=math` through the same logging test-wrapper: libm.py writes out/libm-cmds.txt and gb-min, the
# build tree the guest runs the recorded commands against (at the same absolute path). The i386 libm computes in x87
# code. A math test of the summary without a recorded command fails the build, as recipes/libm. The wrapper also runs
# each test where the builder can run 32-bit code (an x86-64 machine; the Mac's Rosetta cannot): its verdicts are
# kept in log/libm-native.txt and not used. The answers are the x86 reference machine's (tests/correctness/libm32.txt).
#
# libm32-cases.txt: `PART TEST` per program, as recipes/libm (run-libm.sh K/N runs entries K, K+N, ... of
# libm-cmds.txt). The guest's runner is run-libm.sh (the suite's ct word is libm).
set -euo pipefail
. /recipe/correctness/recipes/common.sh
. /recipe/correctness/i386.sh
SUITE=libm32
PARTS=1                                               # tests/correctness/family.toml: parts = 1
if [ "${1:-}" != user ]; then root_phase "$(realpath "$0")" "$SUITE"; exit 0; fi

user_env
sysroot
multilib libm32/multilib-debs.sha256
sources 'glibc=2.41-12+deb13u4' libm32/sources.sha256
play
stop_if_repin

say "building glibc for i686"
S=$W/sysroot; B=x86_64-linux-gnu
export PATH=$S/usr/bin:$PATH LD_LIBRARY_PATH=$W/nativelibs M4=$S/usr/bin/m4 BISON_PKGDATADIR=$S/usr/share/bison
cd "$W"; rm -rf glibc-src gb kh
mkdir glibc-src; tar xJf src/glibc_2.41.orig.tar.xz -C glibc-src --strip-components=1
tar xJf src/glibc_2.41-12+deb13u4.debian.tar.xz -C glibc-src
(cd glibc-src && grep -v '^#' debian/patches/series | while read -r p _; do [ -n "$p" ] && patch -p1 -s < "debian/patches/$p"; done) > "$W/log/glibc-patch.log" 2>&1
mkdir kh; for d in linux asm-generic mtd rdma sound video xen drm misc scsi; do [ -d "$S/usr/include/$d" ] && cp -rL "$S/usr/include/$d" kh/; done
cp -rL "$S/usr/include/$B/asm" kh/asm                 # as remote.sh's glibc stage (asm/ serves both word sizes)
mkdir gb; cd gb
../glibc-src/configure --build=$B --host=i686-linux-gnu --prefix=/usr --with-headers="$W/kh" --disable-werror \
    --enable-stack-protector=strong --enable-bind-now CC="$CC32 -march=i686" CXX=false BUILD_CC="$B-gcc-14 --sysroot=$S" \
    AR=$B-ar AS=$B-as LD=$B-ld NM=$B-nm OBJCOPY=$B-objcopy OBJDUMP=$B-objdump RANLIB=$B-ranlib READELF=$B-readelf \
    STRIP=$B-strip CFLAGS="-O2 -g0" > "$W/log/glibc-configure.log" 2>&1 || { tail -30 "$W/log/glibc-configure.log" >&2; exit 1; }
make -j4 > "$W/log/glibc-make.log" 2>&1 || { tail -30 "$W/log/glibc-make.log" >&2; exit 1; }
cat > "$W/wrap.sh" <<'EOW'
#!/bin/sh
# glibc test-wrapper: log the exact command (cwd and argv) of each test, then run it.
printf '%s\0' "$PWD" "$@" >> "$WRAPLOG.$$"; printf '\n' >> "$WRAPLOG.$$"
exec "$@"
EOW
chmod +x "$W/wrap.sh"; rm -rf "$W/wraplog"; mkdir "$W/wraplog"
WRAPLOG=$W/wraplog/cmd make -k -j4 check subdirs=math test-wrapper="$W/wrap.sh" > "$W/log/glibc-check.log" 2>&1 || true
grep -E '^(PASS|FAIL|XFAIL|XPASS|UNSUPPORTED|ERROR):' math/subdir-tests.sum | sort > "$W/log/libm-native.txt" || true
cd "$W"
python3 -I "$T/libm.py" "$W" > "$W/log/libm-py.log" 2>&1 || { cat "$W/log/libm-py.log" >&2; exit 1; }
cut -f1 "$W/out/libm-cmds.txt" | sort > "$H/cmd-names.txt"
awk '$2 !~ /\/check-/ && $2 != "math/test-tgmath3-macro-list" {print $2}' "$W/log/libm-native.txt" | sort -u > "$H/sum-names.txt"
if [ -n "$(comm -13 "$H/cmd-names.txt" "$H/sum-names.txt")" ]; then
    echo "math tests in the summary without a recorded command:" >&2; comm -13 "$H/cmd-names.txt" "$H/sum-names.txt" | head -20 >&2; exit 1
fi
[ "$(od -An -tx1 -j18 -N1 "$W/gb-min/math/test-double-exp")" = " 03" ] || { echo "test-double-exp is not an i386 program" >&2; exit 1; }
awk -F'\t' -v n=$PARTS '{print (NR - 1) % n "/" n " " $1}' "$W/out/libm-cmds.txt" > "$W/out/$SUITE-cases.txt"
say "$(wc -l < "$W/out/$SUITE-cases.txt") programs listed ($(wc -l < "$H/sum-names.txt") in the summary)"
P=$W/payload; rm -rf "$P"; mkdir -p "$P/ct" "$P$W"
cp -a "$W/gb-min" "$P$W/gb"; cp "$W/out/libm-cmds.txt" "$P/ct/"
image32 "$SUITE" libm > "$W/log/image.log" 2>&1 || { tail -20 "$W/log/image.log" >&2; exit 1; }
