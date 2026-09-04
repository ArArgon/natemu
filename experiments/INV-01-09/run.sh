#!/bin/sh
# Evidence run for INV-01-09. Refuses to run without NATEMU_RAW_ROOT set to
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
    experiments/INV-01-09 docs/research/investigations/INV-01-09.md)
if [ -n "$DIRTY_DETAIL" ]; then
    echo "refusing evidence run: commit the INV-01-09 method and code first" >&2
    echo "$DIRTY_DETAIL" >&2
    exit 2
fi

TS=$(date -u +%Y%m%dT%H%M%SZ)
REV=$(git -C "$REPO_ROOT" rev-parse --short HEAD)
RUN_ID="${TS}-${REV}"
OUT="$RAW_ROOT/INV-01-09/$RUN_ID"
if [ -e "$OUT" ]; then
    echo "refusing to overwrite existing run directory: $OUT" >&2
    exit 2
fi
mkdir -p "$OUT/scratch"

BUILD_DIR="$OUT/build"
"$SCRIPT_DIR/build.sh" "$BUILD_DIR" >"$OUT/build.log" 2>&1
BIN="$BUILD_DIR/probe"
codesign -d --entitlements - "$BIN" >"$OUT/unentitled.codesign.txt" 2>&1 || true

# Run the unentitled normal arm and the checker fault injection before adding
# the restricted entitlement, so code-gate validation cannot be pre-empted by
# entitlement enforcement.
set +e
"$BIN" --label unentitled --scratch-dir "$OUT/scratch" >"$OUT/unentitled.json" 2>"$OUT/unentitled.stderr.txt"
UNENTITLED_RC=$?
set -e
echo "$UNENTITLED_RC" >"$OUT/unentitled.exit.txt"

set +e
python3 "$SCRIPT_DIR/src/check.py" "$OUT/unentitled.json" >"$OUT/unentitled.check.txt" 2>&1
UNENTITLED_CHECK_RC=$?
set -e

set +e
"$BIN" --negative --label negative --scratch-dir "$OUT/scratch" >"$OUT/negative.json" 2>"$OUT/negative.stderr.txt"
NEGATIVE_RC=$?
python3 "$SCRIPT_DIR/src/check.py" "$OUT/negative.json" >"$OUT/negative.check.txt" 2>&1
NEGATIVE_CHECK_RC=$?
set -e
echo "$NEGATIVE_RC" >"$OUT/negative.exit.txt"

# The entitled arm may terminate under platform entitlement enforcement.
# Record its process status and output rather than allowing set -e to erase
# that outcome. If it exits normally, its JSON must pass the integrity checker.
set +e
codesign -s - --entitlements "$SCRIPT_DIR/src/vmnet.entitlements.plist" -f "$BIN" >"$OUT/entitled-sign.stdout.txt" 2>"$OUT/entitled-sign.stderr.txt"
SIGN_RC=$?
set -e
echo "$SIGN_RC" >"$OUT/entitled-sign.exit.txt"
codesign -d --entitlements - "$BIN" >"$OUT/entitled-adhoc.codesign.txt" 2>&1 || true

ENTITLED_RC=125
ENTITLED_CHECK_RC=125
if [ "$SIGN_RC" -eq 0 ]; then
    set +e
    "$BIN" --label entitled-adhoc --scratch-dir "$OUT/scratch" >"$OUT/entitled-adhoc.json" 2>"$OUT/entitled-adhoc.stderr.txt"
    ENTITLED_RC=$?
    set -e
    if [ "$ENTITLED_RC" -eq 0 ]; then
        set +e
        python3 "$SCRIPT_DIR/src/check.py" "$OUT/entitled-adhoc.json" >"$OUT/entitled-adhoc.check.txt" 2>&1
        ENTITLED_CHECK_RC=$?
        set -e
    else
        echo "checker skipped: process exit $ENTITLED_RC; process-level outcome retained" >"$OUT/entitled-adhoc.check.txt"
    fi
else
    echo "checker skipped: entitlement signing failed" >"$OUT/entitled-adhoc.check.txt"
fi
echo "$ENTITLED_RC" >"$OUT/entitled-adhoc.exit.txt"

rmdir "$OUT/scratch" 2>/dev/null || true

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
if [ "$UNENTITLED_RC" -ne 0 ] || [ "$UNENTITLED_CHECK_RC" -ne 0 ]; then
    VALIDATION_RC=1
fi
if [ "$NEGATIVE_RC" -ne 0 ] || [ "$NEGATIVE_CHECK_RC" -eq 0 ]; then
    VALIDATION_RC=1
fi
if [ "$SIGN_RC" -ne 0 ]; then
    VALIDATION_RC=1
fi
if [ "$ENTITLED_RC" -eq 0 ] && [ "$ENTITLED_CHECK_RC" -ne 0 ]; then
    VALIDATION_RC=1
fi
{
    echo "unentitled_process_rc=$UNENTITLED_RC"
    echo "unentitled_checker_rc=$UNENTITLED_CHECK_RC"
    echo "negative_process_rc=$NEGATIVE_RC"
    echo "negative_checker_rc=$NEGATIVE_CHECK_RC"
    echo "entitled_sign_rc=$SIGN_RC"
    echo "entitled_process_rc=$ENTITLED_RC"
    echo "entitled_checker_rc=$ENTITLED_CHECK_RC"
    echo "validation_rc=$VALIDATION_RC"
} >"$OUT/validation.txt"

( cd "$OUT" && find . -type f ! -name SHA256SUMS.txt -exec shasum -a 256 {} \; |
    LC_ALL=C sort -k 2 > SHA256SUMS.txt )

echo "wrote: $OUT" >&2
exit "$VALIDATION_RC"
