#!/bin/sh
# REV-095 independent replication runner for INV-01-01.
# Written by reviewer t01_platform_reviewer; NOT the author's run.sh.
#
# Differences from run.sh that make this an independent execution rather
# than a re-invocation: the approved code is extracted from git at the
# reviewed revision with `git archive` (so a dirty or later working tree
# cannot leak in), the build and signing steps are issued here rather than
# delegated to build.sh, and verification uses this reviewer's own
# verify.py, not the author's check.py.
set -eu
REV=6c0acf5                       # the revision the code gate accepted
HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(git -C "$HERE" rev-parse --show-toplevel)

[ -n "${NATEMU_RAW_ROOT:-}" ] || { echo "refusing: NATEMU_RAW_ROOT unset" >&2; exit 1; }
[ -d "$NATEMU_RAW_ROOT" ] || { echo "refusing: NATEMU_RAW_ROOT not a directory" >&2; exit 1; }
RAW=$(cd "$NATEMU_RAW_ROOT" && pwd -P); ROOT=$(cd "$REPO" && pwd -P)
case "$RAW" in "$ROOT"|"$ROOT"/*) echo "refusing: raw root inside repo" >&2; exit 1;; esac

OUT="$RAW/INV-01-01/replication-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$OUT"
SRC=$(mktemp -d); trap 'rm -rf "$SRC"' EXIT
git -C "$REPO" archive "$REV" experiments/INV-01-01 | tar -x -C "$SRC"
A="$SRC/experiments/INV-01-01"
git -C "$REPO" rev-parse "$REV" > "$OUT/code-revision.txt"

SDK=$(xcrun --show-sdk-path)
clang -Wall -Wextra -O1 -isysroot "$SDK" -framework Hypervisor -o "$SRC/probe" "$A/src/probe.c" 2>"$OUT/build.log"

# unentitled arm
codesign -s - -f "$SRC/probe" >/dev/null 2>&1
"$SRC/probe" --label unentitled > "$OUT/unentitled.json"
codesign -d --entitlements - "$SRC/probe" > "$OUT/unentitled.codesign.txt" 2>&1 || true
# entitled arm: ad-hoc with the approved entitlements plist, as build.sh/run.sh do
codesign -s - --entitlements "$A/src/hypervisor.entitlements.plist" -f "$SRC/probe" >/dev/null 2>&1
"$SRC/probe" --label entitled > "$OUT/entitled.json"
codesign -d --entitlements - "$SRC/probe" > "$OUT/entitled.codesign.txt" 2>&1 || true
# negative control
"$SRC/probe" --negative --label entitled-negative > "$OUT/negative.json"

python3 "$HERE/verify.py" "$OUT/unentitled.json" "$OUT/entitled.json" "$OUT/negative.json" \
    > "$OUT/verify.txt" 2>&1 || true

{ echo "== sw_vers =="; sw_vers
  echo "== sysctl =="; sysctl hw.model hw.memsize hw.ncpu hw.pagesize kern.osversion kern.hv_support
  echo "== clang --version =="; clang --version
  echo "== replicated code revision =="; git -C "$REPO" rev-parse "$REV"
  echo "== resolved SDK =="; xcrun --show-sdk-path; readlink -f "$SDK" 2>/dev/null || true
} > "$OUT/environment.txt" 2>&1

( cd "$OUT" && shasum -a 256 -- * > SHA256SUMS.txt )
echo "$OUT"
