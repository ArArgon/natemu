#!/bin/bash
# cpy-build.sh W J [LOG]: configure and build CPython in W/py (Debian's patched python3.13 source, mtimes uniform) with
# Debian gcc 14 against W/sysroot, `make -j J`, then print one `CT-HASH SHA256 PATH` line per build output (objects,
# libraries, programs, generated sources) and `CT-RESULT cpyb STEP rc=RC sec=S` per step. Run unchanged natively (W the
# AMD machine's work dir) and in the guest (the same absolute W; there W/sysroot is a symlink to /, so the compiler,
# headers and libraries are the image's Debian ones and every command line and path is the native one). Logs to LOG
# (default W/pylog); the guest prints them on the serial line.
set -u
W=$1 J=$2 L=${3:-$1/pylog}; S=$W/sysroot
export PATH=$S/usr/bin:/usr/bin:/bin LD_LIBRARY_PATH=$W/nativelibs SOURCE_DATE_EPOCH=1750000000 LC_ALL=C TZ=UTC \
       PKG_CONFIG_LIBDIR=$S/usr/lib/x86_64-linux-gnu/pkgconfig:$S/usr/share/pkgconfig PKG_CONFIG_SYSROOT_DIR=$S
mkdir -p "$L"; cd "$W/py" || exit 1
step() { local n=$1 t0 t1 rc; shift; t0=$(date +%s.%N); "$@" > "$L/$n.log" 2>&1; rc=$?; t1=$(date +%s.%N)
         echo "CT-RESULT cpyb $n rc=$rc sec=$(echo "$t0 $t1" | awk '{printf "%.1f", $2-$1}')"; return $rc; }
# Debian's common configure arguments (debian/rules) without dtrace, the wheel directory, PGO, LTO and --enable-shared.
step configure ./configure --prefix=/usr --enable-ipv6 --enable-loadable-sqlite-extensions --with-dbmliborder=gdbm \
    --with-computed-gotos --without-ensurepip --with-system-expat --with-ssl-default-suites=openssl --with-system-libmpdec=no \
    CC="x86_64-linux-gnu-gcc-14 --sysroot=$S" OPT="-DNDEBUG -g0 -O3" || exit 1
step make make -j"$J" || exit 1
find . -type f \( -name '*.o' -o -name '*.so' -o -name '*.a' -o -name python -o -name '_bootstrap_python' -o -name '_freeze_module' \
    -o -name '_testembed' -o -name Makefile -o -name pyconfig.h -o -name config.c -o -name '_sysconfigdata*' \
    -o -path './Python/frozen_modules/*' -o -name 'pybuilddir.txt' -o -name 'Setup.local' -o -name 'Setup.stdlib' \) \
    | LC_ALL=C sort | xargs sha256sum | awk '{print "CT-HASH " $1 " " $2}'
