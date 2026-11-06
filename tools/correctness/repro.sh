#!/bin/bash
# repro.sh BASE.cpio.zst DIR OUT: OUT = BASE followed by a zstd cpio holding DIR as /ct/repro (DIR/run.sh is
# run by ct=repro; the kernel unpacks concatenated initramfs archives in order). Boot OUT with suite `repro`.
set -euo pipefail
here=$(dirname "$(realpath "$0")"); t=$(mktemp -d); trap 'rm -rf "$t"' EXIT
mkdir -p "$t/r/ct"; cp -R "$2" "$t/r/ct/repro"; cp "$here/run-repro.sh" "$t/r/ct/"; chmod -R a+rX "$t/r"; chmod 755 "$t/r/ct/run-repro.sh"
python3 "$here/../play/mkcpio.py" "$t/r" "$t/r.cpio"
zstd -q -19 "$t/r.cpio" -o "$t/r.cpio.zst"
pad=$(( (4 - $(stat -f %z "$1") % 4) % 4 ))   # each archive starts on a 4-byte boundary
{ cat "$1"; head -c $pad /dev/zero; cat "$t/r.cpio.zst"; } > "$3"
