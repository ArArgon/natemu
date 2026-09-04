#!/bin/sh
# REV-082 independent replication runner for INV-01-14.
#
# Written by reviewer context `t01_machine_reviewer`. Not the author's run.sh:
# it extracts the approved code at its reviewed revision with `git archive`,
# builds it, drives both arms with this reviewer's own invocation, and
# validates with this reviewer's own `verify.py`. Same raw-root contract as the
# method of record; writes nothing under experiments/.
set -eu
REV=2e6118d
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/../../.." && pwd)

[ -n "${NATEMU_RAW_ROOT:-}" ] || { echo "refusing: NATEMU_RAW_ROOT not set" >&2; exit 2; }
[ -d "$NATEMU_RAW_ROOT" ] || { echo "refusing: NATEMU_RAW_ROOT is not a directory" >&2; exit 2; }
RAW=$(cd "$NATEMU_RAW_ROOT" && pwd)
case "$RAW/" in "$REPO_ROOT"/*) echo "refusing: raw root inside the repository" >&2; exit 2;; esac

OUT="$RAW/INV-01-14/replication-$(date -u +%Y%m%dT%H%M%SZ)"
[ -e "$OUT" ] && { echo "refusing to overwrite $OUT" >&2; exit 2; }
mkdir -p "$OUT/code"

git -C "$REPO_ROOT" archive "$REV" experiments/INV-01-14 | tar -x -C "$OUT/code"
git -C "$REPO_ROOT" rev-parse "$REV" >"$OUT/code-revision.txt"
SRC="$OUT/code/experiments/INV-01-14"
( cd "$SRC" && ./build.sh "$OUT/build" ) >"$OUT/build.log" 2>&1
BIN="$OUT/build/probe"
codesign -d --entitlements - "$BIN" >"$OUT/codesign.txt" 2>&1 || true

run_arm() {
    label=$1; shift
    set +e
    "$BIN" --label "$label" "$@" >"$OUT/$label.json" 2>"$OUT/$label.stderr.txt"
    rc=$?
    set -e
    echo "$rc" >"$OUT/$label.exit.txt"
    return 0
}
run_arm normal
run_arm negative --negative

set +e
python3 "$SCRIPT_DIR/verify.py" normal "$OUT/normal.json" >"$OUT/verify.txt" 2>&1
NOR=$?
python3 "$SCRIPT_DIR/verify.py" negative "$OUT/negative.json" >>"$OUT/verify.txt" 2>&1
NEG=$?
set -e

{
    echo "replication_of=INV-01-14"
    echo "code_revision=$REV"
    echo "reviewer_context=t01_machine_reviewer"
    echo "id=$(id)"
    echo "== sw_vers =="; sw_vers
    echo "== sysctl =="; sysctl hw.model hw.memsize hw.ncpu hw.pagesize kern.osversion
    echo "== clang --version =="; clang --version
    echo "== SDK =="; xcrun --show-sdk-path; xcrun --show-sdk-version
    echo "== uptime =="; uptime
} >"$OUT/environment.txt" 2>&1

{
    echo "normal_verify_rc=$NOR"
    echo "negative_verify_rc=$NEG  # 1 expected: the fabricated field must be rejected"
    echo "normal_exit=$(cat "$OUT/normal.exit.txt")"
    echo "negative_exit=$(cat "$OUT/negative.exit.txt")"
} >"$OUT/summary.txt"

( cd "$OUT" && find . -type f ! -name SHA256SUMS.txt -exec shasum -a 256 {} \; |
    LC_ALL=C sort -k 2 >SHA256SUMS.txt )
echo "wrote: $OUT" >&2
