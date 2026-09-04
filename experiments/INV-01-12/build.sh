#!/bin/sh
# Build the INV-01-12 integrated prototype.
#
#   ./build.sh [output-directory]
#
# Everything generated (the assembled guest object, the generated guest_blob.h
# and the binary) is written to the output directory, never under experiments/.
# Default output directory: ${TMPDIR:-/tmp}/natemu-inv0112-build.

set -eu
here=$(cd "$(dirname "$0")" && pwd)
out=${1:-${TMPDIR:-/tmp}/natemu-inv0112-build}
mkdir -p "$out"

clang -target x86_64-elf -c "$here/prototype/guest.s" -o "$out/guest.o"
python3 "$here/src/elfextract.py" "$out/guest.o" "$out/guest_blob.h"

clang -O2 -std=c11 -Wall -Wextra -Wno-unused-parameter -pthread \
      -I"$out" -o "$out/natemu_proto" "$here/prototype/proto.c"

# Ad-hoc signature with com.apple.security.cs.allow-jit. The unentitled outcome
# is recorded too: run.sh keeps both the entitled binary and an unsigned copy so
# the MAP_JIT dependency (KA-01, owned by Q-01-04) is observable either way.
cp "$out/natemu_proto" "$out/natemu_proto_unentitled"
codesign -s - -f "$out/natemu_proto_unentitled" >/dev/null 2>&1 || true
codesign -s - --entitlements "$here/prototype/jit.entitlements" -f "$out/natemu_proto" >/dev/null 2>&1 \
  || echo "warning: codesign with entitlements failed; binary left ad-hoc signed" >&2

echo "$out/natemu_proto"
