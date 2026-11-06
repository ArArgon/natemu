#!/bin/bash
# overlay.sh BASE.cpio.zst OUT FILE...: OUT = BASE followed by a zstd cpio putting each FILE in /ct (overriding the
# base's copy; the kernel unpacks concatenated initramfs archives in order).
set -euo pipefail
here=$(dirname "$(realpath "$0")"); t=$(mktemp -d); trap 'rm -rf "$t"' EXIT
base=$1 out=$2; shift 2; mkdir -p "$t/r/ct"; cp "$@" "$t/r/ct/"; chmod -R a+rX "$t/r"; chmod 755 "$t"/r/ct/*.sh 2>/dev/null || true
python3 "$here/../play/mkcpio.py" "$t/r" "$t/r.cpio"; zstd -q -19 "$t/r.cpio" -o "$t/r.cpio.zst"
pad=$(( (4 - $(stat -f %z "$base") % 4) % 4 ))
{ cat "$base"; head -c $pad /dev/zero; cat "$t/r.cpio.zst"; } > "$out"
