#!/bin/sh
# REV-095 independent replication runner for INV-01-10.
# Reviewer-written (t01_platform_reviewer); NOT the author's run.sh.
# The approved code is extracted from git at the reviewed revision, and
# verification uses this reviewer's own verify.py, not the author's check.py.
# This runner also captures the host identity the author's run.sh omits.
set -eu
REV=6c0acf5
HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(git -C "$HERE" rev-parse --show-toplevel)

[ -n "${NATEMU_RAW_ROOT:-}" ] || { echo "refusing: NATEMU_RAW_ROOT unset" >&2; exit 1; }
[ -d "$NATEMU_RAW_ROOT" ] || { echo "refusing: NATEMU_RAW_ROOT not a directory" >&2; exit 1; }
RAW=$(cd "$NATEMU_RAW_ROOT" && pwd -P); ROOT=$(cd "$REPO" && pwd -P)
case "$RAW" in "$ROOT"|"$ROOT"/*) echo "refusing: raw root inside repo" >&2; exit 1;; esac

OUT="$RAW/INV-01-10/replication-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$OUT"
SRC=$(mktemp -d); trap 'rm -rf "$SRC"' EXIT
git -C "$REPO" archive "$REV" experiments/INV-01-10 | tar -x -C "$SRC"
A="$SRC/experiments/INV-01-10"
git -C "$REPO" rev-parse "$REV" > "$OUT/code-revision.txt"

sh "$A/src/tools.sh"            > "$OUT/tools.json"
sh "$A/src/tools.sh" --negative > "$OUT/negative.json"
python3 "$HERE/verify.py" "$OUT/tools.json" "$OUT/negative.json" > "$OUT/verify.txt" 2>&1 || true

{ echo "== sw_vers =="; sw_vers
  echo "== sysctl =="; sysctl hw.model hw.memsize hw.ncpu hw.pagesize kern.osversion
  echo "== clang --version =="; clang --version
  echo "== xcode-select -p =="; xcode-select -p
  echo "== replicated code revision =="; git -C "$REPO" rev-parse "$REV"
} > "$OUT/environment.txt" 2>&1

( cd "$OUT" && shasum -a 256 -- * > SHA256SUMS.txt )
echo "$OUT"
