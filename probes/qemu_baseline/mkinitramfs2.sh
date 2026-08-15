#!/bin/bash
# Repack the existing x86_64 initramfs with guest_init2.sh as /init.
# Does not touch build.sh, guest_init.sh or initramfs-x86_64.cpio.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK="$HERE/work"
RD="$WORK/rootfs-x86_64"

if [ ! -d "$RD" ]; then
    echo "work/rootfs-x86_64 missing -- run ./build.sh (and fetch_7zip.sh) first" >&2
    exit 1
fi
cp "$HERE/guest_init2.sh" "$RD/init"
chmod 755 "$RD/init"
OUT="$WORK/initramfs2-x86_64.cpio"
( cd "$RD" && tar --format newc --uid 0 --gid 0 --uname root --gname root -cf "$OUT" . )
echo "$(basename "$OUT")  $(stat -f%z "$OUT") bytes  sha256=$(shasum -a 256 "$OUT" | cut -d' ' -f1)"
