#!/bin/bash
# x86-overlay.sh BASE.cpio.zst OUT ROOT: OUT = BASE followed by a zstd cpio of the tree ROOT (e.g. ROOT/ct/run-x86st.sh,
# ROOT/ct/repro/run.sh), which overrides the base's files: the kernel unpacks concatenated initramfs archives in order.
# As overlay.sh / repro.sh, but takes a tree and also works when BASE's size is a multiple of 4 (no padding needed).
set -euo pipefail
here=$(dirname "$(realpath "$0")"); t=$(mktemp -d); trap 'rm -rf "$t"' EXIT
cp -R "$3" "$t/r"; chmod -R a+rX "$t/r"; find "$t/r" -name '*.sh' -exec chmod 755 {} +
python3 "$here/../play/mkcpio.py" "$t/r" "$t/r.cpio"; zstd -q -19 "$t/r.cpio" -o "$t/r.cpio.zst"
pad=$(( (4 - $(stat -f %z "$1") % 4) % 4 ))
{ cat "$1"; [ $pad = 0 ] || head -c $pad /dev/zero; cat "$t/r.cpio.zst"; } > "$2"
