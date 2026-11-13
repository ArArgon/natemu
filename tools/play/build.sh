#!/bin/bash
# build.sh [HOST] [OUTDIR]: build the play initramfs (a minimal Debian trixie that runs from RAM) and fetch its kernel.
#   HOST    any x86-64 Linux machine reachable by ssh, no root needed (default ubuntu@192.168.12.11); it needs apt-get,
#           dpkg, curl, python3, zstd and internet access to snapshot.debian.org. It builds in a temp dir it deletes.
#   OUTDIR  default ~/Library/Caches/natemu/play; receives bzImage, play.cpio.zst, packages.txt, SHA256SUMS.
# Reproducible: the kernel is the exact Debian package below, checked against pinned SHA-256s; the rootfs packages come
# from the snapshot.debian.org archive date SNAP in rootfs.sh. Re-running replaces the outputs.
set -euo pipefail
here=$(dirname "$(realpath "$0")"); HOST=${1:-ubuntu@192.168.12.11}; OUT=${2:-$HOME/Library/Caches/natemu/play}
KVER=6.12.107; DEB=linux-image-$KVER+deb13-amd64_$KVER-1_amd64.deb
DEB_SHA=7794643cf4560de2a7e3b069e8ca8511ad2a568d652ea285f0fe051f79bc9e99     # the .deb
BZ_SHA=2b2358b37674d2505350528875bb17afae2a36522a9e8a9417eaca65a7da0e08      # its boot/vmlinuz-* (Debian 6.12.107-1)
sha() { shasum -a 256 "$1" | cut -d' ' -f1; }
mkdir -p "$OUT"; tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT

# 1. Kernel: the signed amd64 package (what Debian ships and what natemu has been booting).
if [ "$(sha "$OUT/bzImage" 2>/dev/null)" != "$BZ_SHA" ]; then
    for base in https://deb.debian.org/debian https://snapshot.debian.org/archive/debian/20260915T000000Z; do
        curl -fsSL -o "$tmp/k.deb" "$base/pool/main/l/linux-signed-amd64/$DEB" && break
    done
    [ "$(sha "$tmp/k.deb")" = "$DEB_SHA" ] || { echo "kernel .deb SHA-256 mismatch" >&2; exit 1; }
    (cd "$tmp" && ar x k.deb data.tar.xz && tar xJf data.tar.xz "./boot/vmlinuz-$KVER+deb13-amd64")
    [ "$(sha "$tmp/boot/vmlinuz-$KVER+deb13-amd64")" = "$BZ_SHA" ] || { echo "bzImage SHA-256 mismatch" >&2; exit 1; }
    cp "$tmp/boot/vmlinuz-$KVER+deb13-amd64" "$OUT/bzImage"
fi

# 2. Root filesystem, built on HOST.
W=$(ssh "$HOST" 'mktemp -d'); trap 'rm -rf "$tmp"; ssh "$HOST" "rm -rf $W"' EXIT
tar -C "$here" -c rootfs.sh init mkcpio.py | ssh "$HOST" "tar -x -C $W"
ssh "$HOST" "cd $W && KDEB_SHA=$DEB_SHA bash rootfs.sh $W/b" > "$tmp/build.log" 2>&1 || { tail -30 "$tmp/build.log" >&2; exit 1; }
scp -q "$HOST:$W/b/out/play.cpio.zst" "$HOST:$W/b/out/packages.txt" "$OUT/"
tail -4 "$tmp/build.log"

# 3. Checksums.
(cd "$OUT" && shasum -a 256 bzImage play.cpio.zst packages.txt > SHA256SUMS && cat SHA256SUMS)
