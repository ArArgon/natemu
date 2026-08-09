#!/bin/bash
# Build everything the QEMU TCG baseline measurement needs.
#
#   ./build.sh            # compile bench for 4 targets + build both initramfs
#
# Requires: zig (brew install zig), curl, bsdtar (macOS system tar).
# Downloads Alpine 3.24.1 minirootfs + netboot kernels into ./work/.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK="$HERE/work"
BIN="$HERE/bin"
ALPINE_VER=3.24.1
ALPINE_BRANCH=v3.24
CDN=https://dl-cdn.alpinelinux.org/alpine

mkdir -p "$WORK" "$BIN"

# ---------------------------------------------------------------- downloads
fetch() { # url sha256
    local url="$1" want="$2" out="$WORK/$(basename "$1")"
    if [ ! -f "$out" ]; then
        echo "== fetch $(basename "$1")"
        curl -sS -L -o "$out" "$url"
    fi
    local got
    got=$(shasum -a 256 "$out" | cut -d' ' -f1)
    if [ -n "$want" ] && [ "$got" != "$want" ]; then
        echo "SHA MISMATCH $out: got $got want $want" >&2
        exit 1
    fi
    echo "$(basename "$1")  sha256=$got"
}

fetch "$CDN/$ALPINE_BRANCH/releases/x86_64/alpine-minirootfs-$ALPINE_VER-x86_64.tar.gz" \
      41f73e3cf5fa919b8aa5ca6b30dc48f0da2720776d7423e2a7748211456fe081
fetch "$CDN/$ALPINE_BRANCH/releases/aarch64/alpine-minirootfs-$ALPINE_VER-aarch64.tar.gz" \
      f55a90f69052c5bd6f92cb09a8f47065970830b194c917a006fb94028e721259
fetch "$CDN/$ALPINE_BRANCH/releases/x86_64/alpine-netboot-$ALPINE_VER-x86_64.tar.gz" \
      9a7769ea8fa1737b1b49d82f1bdd53d0a17338d6d3b7cfc6f2c3ec5158596d8b
fetch "$CDN/$ALPINE_BRANCH/releases/aarch64/alpine-netboot-$ALPINE_VER-aarch64.tar.gz" \
      54fe38fa41cce740ba379458ed63cfcd89ab06ae5e6a47a06dafe0a34e8427e8

for A in x86_64 aarch64; do
    if [ ! -f "$WORK/nb-$A/boot/vmlinuz-virt" ]; then
        mkdir -p "$WORK/nb-$A"
        tar -xzf "$WORK/alpine-netboot-$ALPINE_VER-$A.tar.gz" -C "$WORK/nb-$A"
    fi
done

# ---------------------------------------------------------------- bench build
echo "== zig $(zig version)"
ZFLAGS=(-O2 -fno-unroll-loops -g0)
zig cc -target aarch64-macos      "${ZFLAGS[@]}"         -o "$BIN/bench.macos-arm64" "$HERE/bench.c"
zig cc -target x86_64-macos       "${ZFLAGS[@]}"         -o "$BIN/bench.macos-x64"   "$HERE/bench.c"
zig cc -target aarch64-linux-musl "${ZFLAGS[@]}" -static -o "$BIN/bench.linux-arm64" "$HERE/bench.c"
zig cc -target x86_64-linux-musl  "${ZFLAGS[@]}" -static -o "$BIN/bench.linux-x64"   "$HERE/bench.c"
shasum -a 256 "$BIN"/bench.*

# ---------------------------------------------------------------- initramfs
build_initramfs() { # alpine_arch bench_binary out.cpio
    local A="$1" BENCHBIN="$2" OUT="$3"
    local RD="$WORK/rootfs-$A"
    rm -rf "$RD"; mkdir -p "$RD"
    tar -xzf "$WORK/alpine-minirootfs-$ALPINE_VER-$A.tar.gz" -C "$RD" 2>/dev/null || true
    cp "$BENCHBIN" "$RD/bench"; chmod 755 "$RD/bench"
    # optional overlay (7-Zip 26.01 + libstdc++/libgcc), see fetch_7zip.sh
    if [ -d "$WORK/overlay-$A" ]; then cp -a "$WORK/overlay-$A/". "$RD/"; fi
    cp "$HERE/guest_init.sh" "$RD/init"; chmod 755 "$RD/init"
    mkdir -p "$RD/proc" "$RD/sys" "$RD/dev"
    ( cd "$RD" && tar --format newc --uid 0 --gid 0 --uname root --gname root -cf "$OUT" . )
    echo "$(basename "$OUT")  $(stat -f%z "$OUT") bytes  sha256=$(shasum -a 256 "$OUT" | cut -d' ' -f1)"
}

build_initramfs x86_64  "$BIN/bench.linux-x64"   "$WORK/initramfs-x86_64.cpio"
build_initramfs aarch64 "$BIN/bench.linux-arm64" "$WORK/initramfs-aarch64.cpio"

echo "== kernels"
for A in x86_64 aarch64; do
    printf '%-8s %s\n' "$A" "$(shasum -a 256 "$WORK/nb-$A/boot/vmlinuz-virt")"
done
echo "OK"
