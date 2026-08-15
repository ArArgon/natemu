#!/bin/bash
# Fetch an Alpine 3.24 x86_64 gcc toolchain (and make) and unpack it into
# work/overlay-x86_64/, so the guest can run a real compile workload.
# Dependencies are resolved from the APKINDEX rather than hardcoded.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK="$HERE/work"; mkdir -p "$WORK"; cd "$WORK"
BR=v3.24
CDN=https://dl-cdn.alpinelinux.org/alpine
ARCH=x86_64

for repo in main community; do
  d="idx-$repo-$ARCH"
  if [ ! -f "$d/APKINDEX" ]; then
    mkdir -p "$d"
    curl -sS -L -o "APKINDEX-$repo.tar.gz" "$CDN/$BR/$repo/$ARCH/APKINDEX.tar.gz"
    tar -xzf "APKINDEX-$repo.tar.gz" -C "$d"
  fi
done

python3 "$HERE/resolve_apk.py" "$WORK" "$CDN/$BR" "$ARCH" gcc musl-dev make binutils

rm -rf "$WORK/tc-$ARCH"; mkdir -p "$WORK/tc-$ARCH"
for f in "$WORK"/apkcache/*.apk; do
  tar -xzf "$f" -C "$WORK/tc-$ARCH" 2>/dev/null || true
done
rm -rf "$WORK/tc-$ARCH"/.[A-Za-z]* 2>/dev/null || true
du -sh "$WORK/tc-$ARCH"
echo OK
