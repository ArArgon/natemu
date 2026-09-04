#!/bin/sh
# Evidence run for INV-01-14. Refuses to run without NATEMU_RAW_ROOT set to
# an existing directory outside this repository, and writes nothing under
# experiments/. See ../../docs/research/evidence-policy.md and the author
# brief's experiment rules for the contract this script must satisfy.
set -eu
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/../.." && pwd)

if [ -z "${NATEMU_RAW_ROOT:-}" ]; then
    echo "refusing to run: NATEMU_RAW_ROOT is not set" >&2
    exit 2
fi
if [ ! -d "$NATEMU_RAW_ROOT" ]; then
    echo "refusing to run: NATEMU_RAW_ROOT ($NATEMU_RAW_ROOT) is not an existing directory" >&2
    exit 2
fi
RAW_ROOT=$(cd "$NATEMU_RAW_ROOT" && pwd)
case "$RAW_ROOT/" in
    "$REPO_ROOT"/*)
        echo "refusing to run: NATEMU_RAW_ROOT must be outside the repository ($REPO_ROOT)" >&2
        exit 2
        ;;
esac

DIRTY_DETAIL=$(git -C "$REPO_ROOT" status --porcelain -- \
    experiments/INV-01-14 docs/research/investigations/INV-01-14.md)
if [ -n "$DIRTY_DETAIL" ]; then
    echo "refusing evidence run: commit the INV-01-14 method and code first" >&2
    echo "$DIRTY_DETAIL" >&2
    exit 2
fi

TS=$(date -u +%Y%m%dT%H%M%SZ)
REV=$(git -C "$REPO_ROOT" rev-parse --short HEAD)
RUN_ID="${TS}-${REV}"
OUT="$RAW_ROOT/INV-01-14/$RUN_ID"
if [ -e "$OUT" ]; then
    echo "refusing to overwrite existing run directory: $OUT" >&2
    exit 2
fi
mkdir -p "$OUT"

BUILD_DIR="$OUT/build"
"$SCRIPT_DIR/build.sh" "$BUILD_DIR" >"$OUT/build.log" 2>&1
BIN="$BUILD_DIR/probe"
codesign -d --entitlements - "$BIN" >"$OUT/normal.codesign.txt" 2>&1 || true

set +e
"$BIN" --label normal >"$OUT/normal.json" 2>"$OUT/normal.stderr.txt"
NORMAL_RC=$?
python3 "$SCRIPT_DIR/src/check.py" "$OUT/normal.json" >"$OUT/normal.check.txt" 2>&1
NORMAL_CHECK_RC=$?
"$BIN" --negative --label negative >"$OUT/negative.json" 2>"$OUT/negative.stderr.txt"
NEGATIVE_RC=$?
python3 "$SCRIPT_DIR/src/check.py" "$OUT/negative.json" >"$OUT/negative.check.txt" 2>&1
NEGATIVE_CHECK_RC=$?
set -e
echo "$NORMAL_RC" >"$OUT/normal.exit.txt"
echo "$NEGATIVE_RC" >"$OUT/negative.exit.txt"

# Score the negative arm as a demonstrated fault injection only when the
# checker actually parsed the output and rejected it on a rule (exit 1 with
# at least one violated rule reported), not merely "nonzero exit" -- a usage
# error is scored as exit 2 by check.py, never 1, so this also rejects a
# usage error (REV-048 required change 3).
NEGATIVE_DEMONSTRATED=0
if [ "$NEGATIVE_CHECK_RC" -eq 1 ] && grep -q "rule(s) violated" "$OUT/negative.check.txt"; then
    NEGATIVE_DEMONSTRATED=1
fi

{
    echo "run_id=$RUN_ID"
    echo "git_head=$(git -C "$REPO_ROOT" rev-parse HEAD)"
    echo "experiment_tree_dirty_files=0"
    echo "== sw_vers =="
    sw_vers
    echo "== sysctl =="
    sysctl hw.model hw.memsize hw.ncpu hw.pagesize kern.osversion
    echo "== clang --version =="
    clang --version
    echo "== SDK =="
    xcrun --show-sdk-path
    xcrun --show-sdk-version
} >"$OUT/environment.txt" 2>&1

VALIDATION_RC=0
if [ "$NORMAL_RC" -ne 0 ] || [ "$NORMAL_CHECK_RC" -ne 0 ]; then
    VALIDATION_RC=1
fi
if [ "$NEGATIVE_DEMONSTRATED" -ne 1 ]; then
    VALIDATION_RC=1
fi
{
    echo "normal_process_rc=$NORMAL_RC"
    echo "normal_checker_rc=$NORMAL_CHECK_RC"
    echo "negative_process_rc=$NEGATIVE_RC"
    echo "negative_checker_rc=$NEGATIVE_CHECK_RC"
    echo "negative_demonstrated=$NEGATIVE_DEMONSTRATED"
    echo "validation_rc=$VALIDATION_RC"
} >"$OUT/validation.txt"

( cd "$OUT" && find . -type f ! -name SHA256SUMS.txt -exec shasum -a 256 {} \; |
    LC_ALL=C sort -k 2 > SHA256SUMS.txt )

echo "wrote: $OUT" >&2
exit "$VALIDATION_RC"
