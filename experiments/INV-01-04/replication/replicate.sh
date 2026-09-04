#!/bin/sh
# REV-076 independent replication runner for INV-01-04.
# Reviewer context `t01_memory_reviewer`. Not derived from run.sh: it extracts
# the experiment code at its RECORDED revision (`cee40cb`) into a scratch tree,
# builds and runs it there, writes only under $NATEMU_RAW_ROOT, and checks the
# output with this reviewer's own verify.py as well as the author's check.py.
set -eu

INV=INV-01-04
REV=cee40cb
HERE=$(cd "$(dirname "$0")" && pwd -P)
REPO=$(cd "$HERE/../../.." && pwd -P)

: "${NATEMU_RAW_ROOT:?refusing to run: NATEMU_RAW_ROOT is not set}"
[ -d "$NATEMU_RAW_ROOT" ] || { echo "refusing to run: NATEMU_RAW_ROOT is not an existing directory" >&2; exit 2; }
RAW=$(cd "$NATEMU_RAW_ROOT" && pwd -P)
case "$RAW/" in "$REPO"/*) echo "refusing to run: NATEMU_RAW_ROOT is inside the repository" >&2; exit 2 ;; esac

ORIG=${1:?usage: replicate.sh <original result.json from the MAN-01-$n-01 run>}
[ -f "$ORIG" ] || { echo "original result.json not found: $ORIG" >&2; exit 2; }

OUT="$RAW/$INV/replication-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$OUT"

# The recorded revision, not the working tree.
SRC=$(mktemp -d)
trap 'rm -rf "$SRC"' EXIT
git -C "$REPO" archive "$REV" "experiments/$INV" | tar -x -C "$SRC"
echo "$REV" > "$OUT/code-revision.txt"
git -C "$REPO" rev-parse "$REV" >> "$OUT/code-revision.txt"

( cd "$SRC/experiments/$INV" && make BUILD="$OUT" ) > "$OUT/build.log" 2>&1

{
  echo "== replication of"; echo "$ORIG"
  echo "== sw_vers";      sw_vers
  echo "== sysctl";       sysctl hw.model hw.memsize hw.ncpu hw.pagesize kern.osversion
  echo "== loadavg";      sysctl -n vm.loadavg; uptime
  echo "== clang";        clang --version
  echo "== sdk";          xcrun --sdk macosx --show-sdk-path; xcrun --sdk macosx --show-sdk-version
  echo "== code revision"; cat "$OUT/code-revision.txt"
  echo "== repo HEAD";    git -C "$REPO" rev-parse HEAD
  echo "== codesign";     codesign -d -vvv "$OUT/probe" 2>&1 || true
  echo "== entitlements"; codesign -d --entitlements - "$OUT/probe" 2>&1 || true
  echo "== date";         date -u +%Y-%m-%dT%H:%M:%SZ
} > "$OUT/env.txt" 2>&1

"$OUT/probe"            > "$OUT/result.json"
"$OUT/probe" --negative > "$OUT/result-negative.json"

python3 "$SRC/experiments/$INV/src/check.py" "$OUT/result.json"          normal   > "$OUT/check.txt"          2>&1 || true
python3 "$SRC/experiments/$INV/src/check.py" "$OUT/result-negative.json" negative > "$OUT/check-negative.txt" 2>&1 || true
python3 "$HERE/verify.py" "$OUT/result.json" "$ORIG" > "$OUT/verify.txt" 2>&1 || true

echo "author check normal:   $(tail -n1 "$OUT/check.txt")"
echo "author check negative: $(tail -n1 "$OUT/check-negative.txt")  (must be FAIL)"
echo "reviewer verify:       $(tail -n1 "$OUT/verify.txt")"
echo "$OUT"
