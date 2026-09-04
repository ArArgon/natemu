#!/bin/sh
# INV-01-03 evidence run. Writes only under $NATEMU_RAW_ROOT.
set -eu

INV=INV-01-03
REPO=$(cd "$(dirname "$0")/../.." && pwd -P)

: "${NATEMU_RAW_ROOT:?refusing to run: NATEMU_RAW_ROOT is not set}"
[ -d "$NATEMU_RAW_ROOT" ] || { echo "refusing to run: NATEMU_RAW_ROOT is not an existing directory" >&2; exit 2; }
RAW=$(cd "$NATEMU_RAW_ROOT" && pwd -P)
case "$RAW/" in
  "$REPO"/*) echo "refusing to run: NATEMU_RAW_ROOT is inside the repository" >&2; exit 2 ;;
esac

RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)-$(git -C "$REPO" rev-parse --short HEAD)"
OUT="$RAW/$INV/$RUN_ID"
mkdir -p "$OUT"

# Build into the run directory: the measured binary is part of the artifact and
# nothing lands under experiments/.
make -C "$(dirname "$0")" BUILD="$OUT" >"$OUT/build.log" 2>&1

{
  echo "== sw_vers";        sw_vers
  echo "== sysctl";         sysctl hw.model hw.memsize hw.ncpu hw.pagesize kern.osversion
  echo "== clang";          clang --version
  echo "== sdk";            xcrun --sdk macosx --show-sdk-path; xcrun --sdk macosx --show-sdk-version
  echo "== git";            git -C "$REPO" rev-parse HEAD; git -C "$REPO" status --porcelain
  echo "== codesign";       codesign -d -vvv "$OUT/probe" 2>&1 || true
  echo "== entitlements";   codesign -d --entitlements - "$OUT/probe" 2>&1 || true
  echo "== date";           date -u +%Y-%m-%dT%H:%M:%SZ
} >"$OUT/env.txt" 2>&1

"$OUT/probe"            >"$OUT/result.json"
"$OUT/probe" --negative >"$OUT/result-negative.json"

python3 "$(dirname "$0")/src/check.py" "$OUT/result.json"          >"$OUT/check.txt" 2>&1 || true
python3 "$(dirname "$0")/src/check.py" "$OUT/result-negative.json" >"$OUT/check-negative.txt" 2>&1 || true

echo "normal:   $(tail -n1 "$OUT/check.txt")"
echo "negative: $(tail -n1 "$OUT/check-negative.txt")  (must be FAIL)"
echo "$OUT"
