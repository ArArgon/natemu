#!/bin/bash
# stress-remote.sh WORKDIR STAGE...: the stress lane's stages on the reference AMD machine (no root). WORKDIR needs
# remote.sh's `sysroot` and `play` stages first (Debian trixie gcc 14 + libc; the play rootfs in pb/).
#   build      Debian trixie's stress-ng 0.19.02-1 (src/stress-ng_0.19.02{.orig,-1.debian}.tar.xz, checked against the
#              .dsc apt fetched from the signed snapshot Sources) + Debian's patch series, built -static (STATIC=1)
#              with Debian gcc against Debian libc into WORKDIR/st/stress-ng.
#   image      WORKDIR/out/stress.cpio.zst: the play rootfs + the correctness /init + /ct/run-stress.sh,
#              /ct/stress-list.txt and /ct/stress-ng.
#   native P.. run-stress.sh natively as this user for each part P (D = WORKDIR/st) into WORKDIR/out/native-P.txt.
set -euo pipefail
W=$(realpath "$1"); shift; here=$(dirname "$(realpath "$0")"); S=$W/sysroot
export PATH=$S/usr/bin:$PATH LD_LIBRARY_PATH=$W/nativelibs

build() {
    cd "$W"; rm -rf sng; mkdir sng; tar xJf src/stress-ng_0.19.02.orig.tar.xz -C sng --strip-components=1
    tar xJf src/stress-ng_0.19.02-1.debian.tar.xz -C sng
    cd sng; [ -f debian/patches/series ] && while read -r p; do case $p in ""|\#*) continue;; esac; patch -p1 < "debian/patches/$p"; done < debian/patches/series
    make -j4 STATIC=1 CC="x86_64-linux-gnu-gcc-14 --sysroot=$S" > "$W/log/stress-make.log" 2>&1
    mkdir -p "$W/st"; cp stress-ng "$W/st/"; cp "$here/stress-list.txt" "$W/st/"; file "$W/st/stress-ng"
    sha256sum "$W/st/stress-ng" | tee "$W/out/stress-ng.sha256"; "$W/st/stress-ng" --version
    cd "$W"; rm -rf sng
}

image() {
    R=$W/img-stress; rm -rf "$R"; cp -a "$W/pb/rootfs" "$R"; mkdir -p "$R/ct"
    cp "$here/init" "$R/init"; cp "$here/run-stress.sh" "$here/run-repro.sh" "$here/stress-list.txt" "$W/st/stress-ng" "$R/ct/"
    chmod 755 "$R/init" "$R"/ct/*.sh
    python3 "$here/play/mkcpio.py" "$R" "$W/out/stress.cpio"
    zstd -q -19 -T0 -f --rm "$W/out/stress.cpio" -o "$W/out/stress.cpio.zst"; rm -rf "$R"; ls -la "$W/out/stress.cpio.zst"
}

native() {
    cp "$here/stress-list.txt" "$W/st/"
    for p in "$@"; do
        o=$W/out/native-${p//,/_}.txt; export TMPDIR=$W/run/tmp; rm -rf "$W/run"; mkdir -p "$TMPDIR"
        { echo "CT-BEGIN suite=stress part=$p cpus=$(nproc) kernel=$(uname -r) native"
          t0=$(date +%s.%N); sh "$here/run-stress.sh" "$W/st" "$p" || true
          echo "CT-END suite=stress guest_s=$(echo "$t0 $(date +%s.%N)" | awk '{print $2-$1}')"; } > "$o" 2>&1
        rm -rf "$W/run"; echo "$p $(grep -c CT-RESULT "$o")"
    done
}

st=$1; shift; "$st" "$@"
