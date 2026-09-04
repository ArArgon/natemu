#!/bin/sh
# REV-082 independent replication runner for INV-01-09.
#
# Written by reviewer context `t01_machine_reviewer`. This is NOT the author's
# run.sh: it extracts the approved code at its reviewed revision with
# `git archive` (so the tree under test cannot be the working tree), builds it,
# drives the four arms with this reviewer's own invocation, and validates with
# this reviewer's own `verify.py`. Same raw-root contract as the method of
# record: refuses without NATEMU_RAW_ROOT, and writes nothing under experiments/.
set -eu
REV=2e6118d
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/../../.." && pwd)

[ -n "${NATEMU_RAW_ROOT:-}" ] || { echo "refusing: NATEMU_RAW_ROOT not set" >&2; exit 2; }
[ -d "$NATEMU_RAW_ROOT" ] || { echo "refusing: NATEMU_RAW_ROOT is not a directory" >&2; exit 2; }
RAW=$(cd "$NATEMU_RAW_ROOT" && pwd)
case "$RAW/" in "$REPO_ROOT"/*) echo "refusing: raw root inside the repository" >&2; exit 2;; esac

OUT="$RAW/INV-01-09/replication-$(date -u +%Y%m%dT%H%M%SZ)"
[ -e "$OUT" ] && { echo "refusing to overwrite $OUT" >&2; exit 2; }
mkdir -p "$OUT/code" "$OUT/scratch"

# The object under test is the reviewed revision, extracted from Git history,
# not whatever happens to be in the working tree.
git -C "$REPO_ROOT" archive "$REV" experiments/INV-01-09 | tar -x -C "$OUT/code"
git -C "$REPO_ROOT" rev-parse "$REV" >"$OUT/code-revision.txt"
SRC="$OUT/code/experiments/INV-01-09"
( cd "$SRC" && ./build.sh "$OUT/build" ) >"$OUT/build.log" 2>&1
BIN="$OUT/build/probe"

run_arm() {  # run_arm <label> [extra probe flags...]
    label=$1; shift
    set +e
    "$BIN" --label "$label" --scratch-dir "$OUT/scratch" "$@" \
        >"$OUT/$label.json" 2>"$OUT/$label.stderr.txt"
    rc=$?
    set -e
    if [ "$rc" -gt 128 ]; then
        echo "$rc (killed by signal $((rc - 128))/$(kill -l $((rc - 128)) 2>/dev/null || echo '?'))" >"$OUT/$label.exit.txt"
    else
        echo "$rc" >"$OUT/$label.exit.txt"
    fi
    return 0
}

# Arm order follows the method of record: fault injection before any
# entitlement, attribution control before the arm it attributes against.
codesign -d --entitlements - "$BIN" >"$OUT/unentitled.codesign.txt" 2>&1 || true
run_arm unentitled
run_arm negative --negative

codesign -s - --entitlements "$SRC/src/benign.entitlements.plist" -f "$BIN" \
    >"$OUT/benign-sign.txt" 2>&1
codesign -d --entitlements - "$BIN" >"$OUT/benign.codesign.txt" 2>&1 || true
codesign --verify --verbose "$BIN" >"$OUT/benign.verify.txt" 2>&1 || true
run_arm benign

codesign -s - --entitlements "$SRC/src/vmnet.entitlements.plist" -f "$BIN" \
    >"$OUT/restricted-sign.txt" 2>&1
codesign -d --entitlements - "$BIN" >"$OUT/restricted.codesign.txt" 2>&1 || true
codesign --verify --verbose "$BIN" >"$OUT/restricted.verify.txt" 2>&1 || true
run_arm restricted

rmdir "$OUT/scratch" 2>/dev/null || true

# This reviewer's own checker, not the author's.
set +e
python3 "$SCRIPT_DIR/verify.py" unentitled "$OUT/unentitled.json" >"$OUT/verify.txt" 2>&1
UN=$?
python3 "$SCRIPT_DIR/verify.py" negative "$OUT/negative.json" >>"$OUT/verify.txt" 2>&1
NEG=$?
python3 "$SCRIPT_DIR/verify.py" benign "$OUT/benign.json" >>"$OUT/verify.txt" 2>&1
BEN=$?
python3 "$SCRIPT_DIR/verify.py" restricted "$OUT/restricted.json" >>"$OUT/verify.txt" 2>&1
RES=$?
set -e

{
    echo "replication_of=INV-01-09"
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
    echo "unentitled_verify_rc=$UN"
    echo "negative_verify_rc=$NEG"
    echo "benign_verify_rc=$BEN"
    echo "restricted_verify_rc=$RES  # 2 expected: the restricted arm is killed, so no JSON exists"
    echo "unentitled_exit=$(cat "$OUT/unentitled.exit.txt")"
    echo "negative_exit=$(cat "$OUT/negative.exit.txt")"
    echo "benign_exit=$(cat "$OUT/benign.exit.txt")"
    echo "restricted_exit=$(cat "$OUT/restricted.exit.txt")"
} >"$OUT/summary.txt"

( cd "$OUT" && find . -type f ! -name SHA256SUMS.txt -exec shasum -a 256 {} \; |
    LC_ALL=C sort -k 2 >SHA256SUMS.txt )
echo "wrote: $OUT" >&2
