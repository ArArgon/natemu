#!/bin/bash
# Fetch 7-Zip 26.01 for all four environments so that the SAME upstream
# release is benchmarked everywhere:
#   - Alpine 7zip-26.01-r0.apk for linux/x86_64 and linux/aarch64 (guest)
#   - 7z2601-mac.tar.xz universal Mach-O for macOS arm64 (native) and
#     macOS x86_64 (Rosetta 2)
# Produces work/overlay-<arch>/ which build.sh folds into the initramfs.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK="$HERE/work"; mkdir -p "$WORK"; cd "$WORK"
SZV=26.01-r0
BR=v3.24
CDN=https://dl-cdn.alpinelinux.org/alpine

for A in x86_64 aarch64; do
  mkdir -p "apk-$A"
  for pkg in "7zip-$SZV" "libstdc++-15.2.0-r5" "libgcc-15.2.0-r5"; do
    f="$pkg-$A.apk"
    [ -f "$f" ] || curl -sS -L -o "$f" "$CDN/$BR/main/$A/$pkg.apk"
    tar -xzf "$f" -C "apk-$A" 2>/dev/null || true
  done
  rm -rf "overlay-$A"; mkdir -p "overlay-$A/usr/lib"
  cp "apk-$A/usr/bin/7zz" "overlay-$A/7zz"; chmod 755 "overlay-$A/7zz"
  cp -a "apk-$A"/usr/lib/libstdc++.so.6* "apk-$A"/usr/lib/libgcc_s.so.1 "overlay-$A/usr/lib/"
done

# QEMU 11.0.1 linux-user (statically linked, no deps) for the aarch64 guest
[ -f qemu-x86_64-11.0.1-r0-aarch64.apk ] || curl -sS -L -o qemu-x86_64-11.0.1-r0-aarch64.apk \
  "$CDN/$BR/community/aarch64/qemu-x86_64-11.0.1-r0.apk"
mkdir -p qu && tar -xzf qemu-x86_64-11.0.1-r0-aarch64.apk -C qu 2>/dev/null || true
cp qu/usr/bin/qemu-x86_64 overlay-aarch64/qemu-x86_64; chmod 755 overlay-aarch64/qemu-x86_64
cp "$HERE/bin/bench.linux-x64" overlay-aarch64/bench-x64; chmod 755 overlay-aarch64/bench-x64

[ -f 7z2601-mac.tar.xz ] || curl -sS -L -O https://www.7-zip.org/a/7z2601-mac.tar.xz
mkdir -p mac7z && tar -xJf 7z2601-mac.tar.xz -C mac7z
shasum -a 256 7z2601-mac.tar.xz 7zip-$SZV-*.apk mac7z/7zz
echo OK
