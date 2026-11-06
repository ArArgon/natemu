#!/bin/bash
# remote.sh WORKDIR STAGE...: runs on the x86-64 build host (the reference AMD machine, no root) for build.sh.
# Stages, in order (each re-runnable):
#   sysroot  Debian trixie packages (snapshot SNAP, as tools/play) unpacked with dpkg -x: Debian gcc 14 and friends.
#   src      sources, checked against pinned SHA-256s (Debian's own source packages through apt where Debian has them).
#   ltp      LTP, built with Debian gcc against Debian libc (dynamic; Debian's libc6 is in the guest image).
#   torture  gcc.c-torture/execute (+ ieee/) of Debian's gcc-14 source, each test at -O0 and -O2, -static.
#   glibc    Debian's glibc 2.41 source built in WORKDIR/gb, then `make check subdirs=math` natively, recording each
#            test's exact command line (test-wrapper) so the guest runs the same command against the same build tree.
#   play     the play rootfs (tools/play/rootfs.sh) as the image base.
#   image    WORKDIR/out/{ltp,torture,libm}.cpio.zst: the play rootfs + zstd + one suite + the runner /init.
# Native runs are runner.sh's job (natively.sh). Needs apt-get, dpkg, curl, python3, zstd, patch, perl, gawk.
set -euo pipefail
W=$(realpath "$1"); shift; here=$(dirname "$(realpath "$0")"); S=$W/sysroot
SNAP=${SNAP:-20260915T000000Z}
mkdir -p "$W"/{src,out,log}
export APT_CONFIG=$W/apt/apt.conf

sysroot() {
    mkdir -p "$W"/{apt/lists/partial,apt/cache/archives/partial,apt/etc/preferences.d,apt/etc/apt.conf.d,debs}
    cd "$W/debs"
    [ -d "$W/kr" ] || { apt-get download debian-archive-keyring >/dev/null; dpkg -x debian-archive-keyring_*.deb "$W/kr"; rm debian-archive-keyring_*.deb; }
    cat > "$W/apt/apt.conf" <<EOC
Dir::State "$W/apt"; Dir::State::Lists "$W/apt/lists"; Dir::State::status "$W/apt/status"; Dir::Cache "$W/apt/cache";
Dir::Etc::SourceList "$W/apt/sources.list"; Dir::Etc::SourceParts "$W/apt/etc/none";
Dir::Etc::Preferences "$W/apt/etc/preferences"; Dir::Etc::PreferencesParts "$W/apt/etc/preferences.d";
Dir::Etc::Parts "$W/apt/etc/apt.conf.d"; APT::Architecture "amd64"; APT::Architectures { "amd64"; };
APT::Architecture-Variants "amd64"; Debug::NoLocking "true"; APT::Install-Recommends "false"; Acquire::Check-Valid-Until "false"; Acquire::Retries "5";
EOC
    touch "$W/apt/status"
    printf 'deb [signed-by=%s] https://snapshot.debian.org/archive/debian/%s/ trixie main\ndeb-src [signed-by=%s] https://snapshot.debian.org/archive/debian/%s/ trixie main\n' \
        "$W/kr/usr/share/keyrings/debian-archive-keyring.pgp" "$SNAP" "$W/kr/usr/share/keyrings/debian-archive-keyring.pgp" "$SNAP" > "$W/apt/sources.list"
    apt-get update -qq
    rm -f ./*.deb
    apt-get -y -qq -d install --no-install-recommends gcc-14 libc6-dev make bison m4 flex gawk zstd pkgconf
    cp "$W"/apt/cache/archives/*.deb .
    for d in *.deb; do echo "$(dpkg-deb -f "$d" Package)=$(dpkg-deb -f "$d" Version)"; done | sort > "$W/out/sysroot-packages.txt"
    sha256sum ./*.deb > "$W/out/sysroot-debs.sha256"
    rm -rf "$S"; mkdir -p "$S"; for d in ./*.deb; do dpkg -x "$d" "$S"; done
    for l in lib lib64 bin sbin; do [ -L "$S/$l" ] || { [ -d "$S/$l" ] && cp -a "$S/$l/." "$S/usr/$l/" && rm -rf "$S/$l"; ln -sfn usr/$l "$S/$l"; }; done
    # Debian's non-libc shared libraries for the Debian tools run in place (the host's newer glibc runs them).
    NL=$W/nativelibs; rm -rf "$NL"; mkdir -p "$NL"
    for f in "$S"/usr/lib/x86_64-linux-gnu/*.so*; do
        case ${f##*/} in libc.so*|libm.so*|libmvec*|libpthread*|libdl*|librt*|ld-*|libresolv*|libutil*|libanl*|libnss*|libthread_db*|libcrypt*|libBrokenLocale*|libmemusage*|libpcprofile*|libc_malloc*) ;;
        *) ln -sf "$f" "$NL/";; esac
    done
    "$S/usr/bin/x86_64-linux-gnu-gcc-14" --version | head -1
}

tc() {   # the Debian toolchain environment
    export PATH=$S/usr/bin:$PATH LD_LIBRARY_PATH=$W/nativelibs M4=$S/usr/bin/m4 BISON_PKGDATADIR=$S/usr/share/bison
    CC="x86_64-linux-gnu-gcc-14 --sysroot=$S"
}

fetch() { [ -f "$2" ] || curl -sSLf -o "$2" "$1"; echo "$3  $2" | sha256sum -c --quiet; }
src() {
    cd "$W/src"
    # Debian source packages (apt checks them against the signed snapshot Sources).
    for p in gcc-14 glibc; do ls ${p}_*.dsc >/dev/null 2>&1 || apt-get source -d -qq "$p"; done
    # LTP is not packaged by Debian: the upstream release current at trixie's release (13.0, August 9, 2025).
    fetch https://github.com/linux-test-project/ltp/releases/download/20250530/ltp-full-20250530.tar.xz ltp-full-20250530.tar.xz \
          "$(cat "$here/ltp.sha256")"
    sha256sum ./* > "$W/out/src.sha256"
}

ltp() {
    tc; cd "$W"; rm -rf ltp-src ltp-inst; tar xJf src/ltp-full-20250530.tar.xz; mv ltp-full-20250530 ltp-src; cd ltp-src
    # listmount04 needs struct mnt_id_req.spare (headers newer than trixie's linux-libc-dev 6.12): not built.
    rm testcases/kernel/syscalls/listmount/listmount04.c
    ./configure --prefix=/opt/ltp --without-modules --without-numa --without-tirpc CC="$CC" CFLAGS="-O2 -g0" > "$W/log/ltp-configure.log" 2>&1
    make -k -j4 > "$W/log/ltp-make.log" 2>&1 || true
    make -k install DESTDIR="$W/ltp-inst" > "$W/log/ltp-install.log" 2>&1 || true
    cd "$W"; rm -rf ltp-src
    du -sh ltp-inst; ls ltp-inst/opt/ltp/testcases/bin | wc -l
}

torture() {
    tc; cd "$W"; rm -rf gcc-t torture; mkdir -p gcc-t torture/bin
    o=$(ls src/gcc-14_*.orig.tar.gz); tar xzf "$o" -C gcc-t
    inner=$(ls gcc-t/*/gcc-*.tar.*)
    # execute/ and the five gcc.dg files that execute tests #include ("../../gcc.dg/...").
    tar xf "$inner" -C gcc-t --wildcards '*/gcc/testsuite/gcc.c-torture/execute/*' '*/gcc/testsuite/gcc.dg/tree-ssa/pr105777.c' \
        '*/gcc/testsuite/gcc.dg/tree-ssa/pr109938.c' '*/gcc/testsuite/gcc.dg/tree-ssa/pr109986.c' '*/gcc/testsuite/gcc.dg/tree-ssa/pr30314.c' \
        '*/gcc/testsuite/gcc.dg/pr98304-1.c'
    mkdir -p torture/gcc.c-torture; cp -a gcc-t/*/gcc/testsuite/gcc.c-torture/execute torture/gcc.c-torture/
    cp -a gcc-t/*/gcc/testsuite/gcc.dg torture/; rm -rf gcc-t
    python3 -I "$here/torture.py" build "$W/torture" "$CC" > "$W/log/torture-build.log" 2>&1
    tail -3 "$W/log/torture-build.log"
}

glibc() {
    tc; cd "$W"; rm -rf glibc-src gb
    dsc=$(ls src/glibc_*.dsc); v=$(sed -n 's/^Version: //p' "$dsc")
    mkdir glibc-src; tar xJf src/glibc_*.orig.tar.xz -C glibc-src --strip-components=1
    tar xJf src/glibc_"$v".debian.tar.xz -C glibc-src
    (cd glibc-src && grep -v '^#' debian/patches/series | while read -r p _; do [ -n "$p" ] && patch -p1 -s < "debian/patches/$p"; done) > "$W/log/glibc-patch.log" 2>&1
    # Kernel headers (Debian linux-libc-dev 6.12) in one tree: glibc's -nostdinc build does not search the multiarch asm/.
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
    WRAPLOG=$W/wraplog/cmd make -k -j4 check subdirs=math test-wrapper="$W/wrap.sh" > "$W/log/glibc-check.log" 2>&1 || true
    grep -E '^(PASS|FAIL|XFAIL|XPASS|UNSUPPORTED|ERROR):' math/subdir-tests.sum | sort > "$W/out/libm-native.txt" || true
    awk '{print $1}' "$W/out/libm-native.txt" | sort | uniq -c
}

openssl() {   # Debian's openssl 3.5.7 source (+ Debian patches), static, built in WORKDIR/ob (tests run in place)
    tc; cd "$W"; rm -rf ob
    mkdir ob; tar xzf src/openssl_3.5.7.orig.tar.gz -C ob --strip-components=1; tar xJf src/openssl_3.5.7-1~deb13u2.debian.tar.xz -C ob
    (cd ob && grep -v '^#' debian/patches/series | while read -r p _; do [ -n "$p" ] && patch -p1 -s < "debian/patches/$p"; done) > "$W/log/openssl-patch.log" 2>&1
    cd ob
    ./Configure linux-x86_64 no-shared no-module no-dso -static --prefix=/usr CC="$CC" > "$W/log/openssl-configure.log" 2>&1
    make -j4 > "$W/log/openssl-make.log" 2>&1
    du -sh "$W/ob"
}

play() {
    bash "$here/play/rootfs.sh" "$W/pb" > "$W/log/play.log" 2>&1; tail -2 "$W/log/play.log"
    # zstd (the suites travel compressed and are unpacked at run time) and patch-free extras from the same snapshot.
    APT_CONFIG=$W/pb/apt/apt.conf apt-get -y -qq -d install --no-install-recommends zstd
    for d in "$W"/pb/apt/cache/archives/*.deb; do   # zstd and the libraries it adds to play's package set
        grep -qx "$(dpkg-deb -f "$d" Package)=$(dpkg-deb -f "$d" Version)" "$W/pb/out/packages.txt" || { echo "added $(basename "$d")"; dpkg -x "$d" "$W/pb/rootfs"; }
    done
    rm -f "$W/pb/out/play.cpio.zst"
}

image() {   # image SUITE...: base rootfs + suite payload + /init (+ the kernel's config in /boot, for LTP's kconfig checks)
    K=linux-image-6.12.107+deb13-amd64_6.12.107-1_amd64.deb   # the play kernel's package (SHA-256 pinned in tools/play/build.sh)
    if [ ! -f "$W/kconfig" ]; then
        for base in https://deb.debian.org/debian https://snapshot.debian.org/archive/debian/20260915T000000Z; do
            curl -fsSL -o "$W/k.deb" "$base/pool/main/l/linux-signed-amd64/$K" && break
        done
        echo "7794643cf4560de2a7e3b069e8ca8511ad2a568d652ea285f0fe051f79bc9e99  $W/k.deb" | sha256sum -c --quiet
        (cd "$W" && rm -rf kx && mkdir kx && dpkg -x k.deb kx && cp kx/boot/config-6.12.107+deb13-amd64 kconfig && rm -rf kx k.deb)
    fi
    for s in "$@"; do
        R=$W/img-$s; rm -rf "$R"; cp -a "$W/pb/rootfs" "$R"; mkdir -p "$R/ct" "$R/boot"
        cp "$W/kconfig" "$R/boot/config-6.12.107+deb13-amd64"
        cp "$here/init" "$R/init"; cp "$here/run-$s.sh" "$here/run-repro.sh" "$R/ct/"; chmod 755 "$R/init" "$R"/ct/run-*.sh
        case $s in
            ltp) L=$W/ltp-inst/opt/ltp; mkdir -p "$R/opt/ltp/testcases/bin"   # the kept programs (ltp-list.py), stripped
                 (cd "$L" && tar cf - --exclude=./testcases/bin .) | tar xf - -C "$R/opt/ltp"
                 (cd "$L/testcases/bin" && tar cf - $(cat "$W/out/ltp-keep.txt")) | tar xf - -C "$R/opt/ltp/testcases/bin"
                 (tc; cd "$R/opt/ltp/testcases/bin" && for f in *; do [ "$(head -c4 "$f")" = "$(printf '\177ELF')" ] && x86_64-linux-gnu-strip "$f"; done; true)
                 cp "$W/out/ltp-list.txt" "$R/ct/";;
            torture) cp "$W"/torture/tests-*.tar.zst "$W/torture/list.txt" "$R/ct/";;
            openssl) # perl (OpenSSL's harness) from play's snapshot, and the build tree minus objects and unused programs
                 APT_CONFIG=$W/pb/apt/apt.conf apt-get -y -qq -d install --no-install-recommends perl
                 for d in "$W"/pb/apt/cache/archives/*.deb; do
                     grep -qx "$(dpkg-deb -f "$d" Package)=$(dpkg-deb -f "$d" Version)" "$W/pb/out/packages.txt" || dpkg -x "$d" "$R"
                 done
                 mkdir -p "$R$W/ob"
                 (cd "$W/ob" && find . -type f ! -name '*.o' ! -name '*.a' ! -name '*.d' ! -path './fuzz/*' ! -path './doc/*' ! -path './demos/*' \
                     | while read -r f; do if [ "$(head -c4 "$f")" = "$(printf '\177ELF')" ]; then grep -qx "${f#./}" "$here/openssl-progs.txt" && echo "$f"; else echo "$f"; fi; done \
                     | tar cf - -T -) | tar xf - -C "$R$W/ob"
                 (tc; for f in $(cat "$here/openssl-progs.txt"); do x86_64-linux-gnu-strip "$R$W/ob/$f"; done)
                 cp "$here/openssl-tests.txt" "$here/openssl-masks.txt" "$R/ct/"; du -sh "$R$W/ob";;
            libm) mkdir -p "$R$W"; (cd "$W" && tar cf - gb-min) | tar xf - -C "$R$W"; mv "$R$W/gb-min" "$R$W/gb"
                  cp "$W/out/libm-cmds.txt" "$R/ct/";;
        esac
        python3 "$here/play/mkcpio.py" "$R" "$W/out/$s.cpio"
        zstd -q -19 -T0 -f --rm "$W/out/$s.cpio" -o "$W/out/$s.cpio.zst"; rm -rf "$R"
        ls -la "$W/out/$s.cpio.zst"
    done
}

st=$1; shift; "$st" "$@"
