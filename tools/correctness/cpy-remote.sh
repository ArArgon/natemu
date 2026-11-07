#!/bin/bash
# cpy-remote.sh WORKDIR STAGE...: the CPython / glibc-string lane's stages on the reference AMD machine (no root).
# WORKDIR needs remote.sh's `sysroot` and `play` stages first, and src/ holding Debian's python3.13 and glibc sources
# (`apt-get source -d python3.13 glibc` with WORKDIR's apt configuration: checked against the signed snapshot Sources).
#   deps      CPython's build dependencies (Debian trixie, snapshot SNAP) unpacked into the sysroot; the list in out/.
#   pysrc     WORKDIR/pysrc: Debian's python3.13 3.13.5-2+deb13u5 (orig + Debian's patch series), every mtime set to
#             SOURCE_DATE_EPOCH (the guest image's cpio has uniform mtimes too); WORKDIR/py a copy to build in.
#   pybuild   cpy-build.sh natively in WORKDIR/py (make -j4) into out/native-cpyb.txt.
#   pytest K/N.. run-cpy.sh natively (the native build; all parts when none given) into out/native-cpy[-K].txt.
#   imgb      out/cpyb.cpio.zst: play rootfs + the sysroot's and deps' packages + the pristine source at WORKDIR/py,
#             WORKDIR/sysroot -> /, /ct/run-cpyb.sh, /ct/cpy-build.sh (the guest builds CPython as natively).
#   imgt      out/cpyt.cpio.zst: play rootfs + deps' runtime packages + the native build tree minus objects at
#             WORKDIR/py + /ct/run-cpy.sh, /ct/cpy-tests.txt (the guest runs the native build's test suite).
#   glibc     Debian's glibc 2.41 built in WORKDIR/gb as remote.sh's glibc stage, then `make check subdirs=string` and
#             `subdirs=wcsmbs` natively through a logging test-wrapper; gstr.py then writes out/gstr-cmds.txt, gb-min.
#   imgg      out/gstr.cpio.zst: play rootfs + gb-min at WORKDIR/gb + /ct/run-gstr.sh, /ct/gstr-cmds.txt.
#   gnative [TAG]  run-gstr.sh natively (the build tree in WORKDIR/gb) into out/native-gstr[-TAG].txt.
set -euo pipefail
W=$(realpath "$1"); shift; here=$(dirname "$(realpath "$0")"); S=$W/sysroot
export APT_CONFIG=$W/apt/apt.conf
SDE=1750000000
# Build dependencies of Debian's python3.13 that this build uses (debian/control), and what its tests read (tzdata,
# netbase, media-types); not tk, bluetooth, db, nsl, locales-all, valgrind, systemtap (not built or not needed).
DEPS="libreadline-dev libncurses-dev zlib1g-dev libbz2-dev liblzma-dev libgdbm-dev libgdbm-compat-dev libssl-dev
      libexpat1-dev libsqlite3-dev libffi-dev uuid-dev tzdata netbase media-types"

deps() {
    cd "$W/apt/cache/archives"; rm -f ./*.deb
    apt-get -y -qq -d install --no-install-recommends $DEPS
    for d in *.deb; do
        p="$(dpkg-deb -f "$d" Package)=$(dpkg-deb -f "$d" Version)"
        grep -qx "$p" "$W/out/sysroot-packages.txt" || { echo "$p"; dpkg -x "$d" "$S"; }
    done | sort > "$W/out/cpy-deps-packages.txt"
    mkdir -p "$W/debs-deps"; cp ./*.deb "$W/debs-deps/"; sha256sum ./*.deb > "$W/out/cpy-deps-debs.sha256"
    for f in "$S"/usr/lib/x86_64-linux-gnu/*.so*; do
        case ${f##*/} in libc.so*|libm.so*|libmvec*|libpthread*|libdl*|librt*|ld-*|libresolv*|libutil*|libanl*|libnss*|libthread_db*|libcrypt*|libBrokenLocale*|libmemusage*|libpcprofile*|libc_malloc*) ;;
        *) [ -e "$W/nativelibs/${f##*/}" ] || ln -sf "$f" "$W/nativelibs/";; esac
    done
    wc -l < "$W/out/cpy-deps-packages.txt"
}

pysrc() {
    cd "$W"; rm -rf pysrc py; mkdir pysrc
    tar xJf src/python3.13_3.13.5.orig.tar.xz -C pysrc --strip-components=1
    tar xJf src/python3.13_3.13.5-2+deb13u5.debian.tar.xz -C pysrc
    (cd pysrc && grep -v '^#' debian/patches/series | while read -r p _; do [ -n "$p" ] && patch -p1 -s < "debian/patches/$p"; done) > "$W/log/py-patch.log" 2>&1
    rm -rf pysrc/debian; find pysrc -exec touch -h -d @$SDE {} +
    cp -a pysrc py; du -sh pysrc
}

pybuild() { rm -rf "$W/pylog"; bash "$here/cpy-build.sh" "$W" 4 > "$W/out/native-cpyb.txt" 2>&1; grep CT-RESULT "$W/out/native-cpyb.txt"; grep -c CT-HASH "$W/out/native-cpyb.txt"; }

pytest() {
    export TMPDIR=$W/run/tmp LD_LIBRARY_PATH=$W/nativelibs   # Debian's libssl, libsqlite3, ... as in the guest
    for p in "${@:-all}"; do
        rm -rf "$W/run"; mkdir -p "$TMPDIR"; o=$W/out/native-cpy${p:+-${p//\//_}}.txt
        { echo "CT-BEGIN suite=cpy part=$p cpus=$(nproc) kernel=$(uname -r) native"
          # A background job of a non-interactive shell (nohup ... &) inherits SIGINT and SIGQUIT ignored; Python then leaves
          # SIGINT ignored and _thread.interrupt_main() does nothing (test_threading's gh-102056 test spins forever).
          t0=$(date +%s.%N); PY=$W/py perl -e '$SIG{$_} = "DEFAULT" for qw(INT QUIT HUP); exec @ARGV' bash "$here/run-cpy.sh" "$W/out" "$p" || true
          echo "CT-END suite=cpy guest_s=$(echo "$t0 $(date +%s.%N)" | awk '{print $2-$1}')"; } > "$o" 2>&1
        echo "$p $(grep -c CT-RESULT "$o")"
    done
    rm -rf "$W/run"
}

base() {   # base ROOT DEBDIR...: play rootfs + every package of DEBDIR not in play's set
    rm -rf "$1"; cp -a "$W/pb/rootfs" "$1"; local r=$1; shift
    for dd in "$@"; do for d in "$dd"/*.deb; do
        grep -qx "$(dpkg-deb -f "$d" Package)=$(dpkg-deb -f "$d" Version)" "$W/pb/out/packages.txt" || dpkg -x "$d" "$r"
    done; done
    rm -rf "$r"/usr/share/doc "$r"/usr/share/man "$r"/usr/share/info "$r"/usr/share/locale
    mkdir -p "$r/ct"; cp "$here/init" "$r/init"; cp "$here/run-repro.sh" "$r/ct/"; chmod 755 "$r/init"
}
pack() { python3 "$here/play/mkcpio.py" "$1" "$W/out/$2.cpio"; zstd -q -19 -T0 -f --rm "$W/out/$2.cpio" -o "$W/out/$2.cpio.zst"; rm -rf "$1"; ls -la "$W/out/$2.cpio.zst"; }

imgb() {
    R=$W/img-cpyb; base "$R" "$W/debs" "$W/debs-deps"
    mkdir -p "$R$W"; cp -a "$W/pysrc" "$R$W/py"; ln -s / "$R$S"
    cp "$here/run-cpyb.sh" "$here/cpy-build.sh" "$here/run-cpy.sh" "$W/out/cpy-tests.txt" "$R/ct/"; chmod 755 "$R"/ct/*.sh
    pack "$R" cpyb
}

imgt() {
    R=$W/img-cpyt; base "$R" "$W/debs-deps"
    mkdir -p "$R$W"; (cd "$W" && tar cf - --exclude='*.o' --exclude='*.a' py) | tar xf - -C "$R$W"
    cp "$here/run-cpy.sh" "$W/out/cpy-tests.txt" "$R/ct/"; chmod 755 "$R"/ct/*.sh
    pack "$R" cpyt
}

glibc() {
    export PATH=$S/usr/bin:$PATH LD_LIBRARY_PATH=$W/nativelibs M4=$S/usr/bin/m4 BISON_PKGDATADIR=$S/usr/share/bison
    CC="x86_64-linux-gnu-gcc-14 --sysroot=$S"; cd "$W"; rm -rf glibc-src gb
    dsc=$(ls src/glibc_*.dsc); v=$(sed -n 's/^Version: //p' "$dsc")
    mkdir glibc-src; tar xJf src/glibc_*.orig.tar.xz -C glibc-src --strip-components=1
    tar xJf src/glibc_"$v".debian.tar.xz -C glibc-src
    (cd glibc-src && grep -v '^#' debian/patches/series | while read -r p _; do [ -n "$p" ] && patch -p1 -s < "debian/patches/$p"; done) > "$W/log/glibc-patch.log" 2>&1
    rm -rf kh; mkdir kh; for d in linux asm-generic mtd rdma sound video xen drm misc scsi; do [ -d "$S/usr/include/$d" ] && cp -rL "$S/usr/include/$d" kh/; done
    cp -rL "$S/usr/include/x86_64-linux-gnu/asm" kh/asm
    mkdir gb; cd gb
    ../glibc-src/configure --prefix=/usr --with-headers="$W/kh" --disable-werror --enable-stack-protector=strong \
        --enable-bind-now CC="$CC" CXX=false CFLAGS="-O2 -g0" > "$W/log/glibc-configure.log" 2>&1
    make -j4 > "$W/log/glibc-make.log" 2>&1
    cat > "$W/wrap.sh" <<'EOW'
#!/bin/sh
# glibc test-wrapper: log the exact command (cwd and argv) of each test, then run it.
printf '%s\0' "$PWD" "$@" >> "$WRAPLOG.$$"; printf '\n' >> "$WRAPLOG.$$"
exec "$@"
EOW
    chmod +x "$W/wrap.sh"; rm -rf "$W/wraplog"; mkdir "$W/wraplog"
    for s in string wcsmbs; do
        WRAPLOG=$W/wraplog/cmd make -k -j4 check subdirs=$s test-wrapper="$W/wrap.sh" > "$W/log/glibc-check-$s.log" 2>&1 || true
        grep -E '^(PASS|FAIL|XFAIL|XPASS|UNSUPPORTED|ERROR):' $s/subdir-tests.sum | sort > "$W/out/gstr-native-sum-$s.txt" || true
        awk '{print $1}' "$W/out/gstr-native-sum-$s.txt" | sort | uniq -c
    done
    python3 -I "$here/gstr.py" "$W"
}

imgg() {
    R=$W/img-gstr; base "$R"
    mkdir -p "$R$W"; (cd "$W" && tar cf - gb-min) | tar xf - -C "$R$W"; mv "$R$W/gb-min" "$R$W/gb"
    cp "$here/run-gstr.sh" "$W/out/gstr-cmds.txt" "$R/ct/"; chmod 755 "$R"/ct/*.sh
    pack "$R" gstr
}

gnative() {
    export TMPDIR=$W/run/tmp; rm -rf "$W/run"; mkdir -p "$TMPDIR"; o=$W/out/native-gstr${1:+-$1}.txt
    { echo "CT-BEGIN suite=gstr part=0/1 cpus=$(nproc) kernel=$(uname -r) native"
      t0=$(date +%s.%N); sh "$here/run-gstr.sh" "$W/out" 0/1 || true
      echo "CT-END suite=gstr guest_s=$(echo "$t0 $(date +%s.%N)" | awk '{print $2-$1}')"; } > "$o" 2>&1
    rm -rf "$W/run"; grep -c CT-RESULT "$o"
}

st=$1; shift; "$st" "$@"
