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

# decode_signal RC: appends " (signal NAME)" to stderr-visible text when RC
# indicates the process was killed by a signal (sh convention: 128+signum).
decode_rc() {
    rc=$1
    if [ "$rc" -gt 128 ] 2>/dev/null; then
        sig=$((rc - 128))
        name=$(kill -l "$sig" 2>/dev/null || echo "$sig")
        echo "$rc (killed by signal $sig/$name)"
    else
        echo "$rc"
    fi
}

# --- executing identity (BP-01-09-start measurement-failure-mode requirement:
#     record UID for every unprivileged/EPERM claim, notably SOCK_RAW) ---
{
    echo "id=$(id)"
    echo "id_u=$(id -u)"
    echo "== TCC/permission grant state =="
    echo "Not captured: no public, unprivileged command-line tool reads this"
    echo "account's TCC grant database on this macOS release without"
    echo "itself requiring a grant (tccutil/sqlite3-on-TCC.db need Full Disk"
    echo "Access). This is a stated method decision, not a residual gap: the"
    echo "SOCK_RAW EPERM result below establishes that this UID lacks raw-"
    echo "socket privilege under its ambient state at run time; it does not"
    echo "establish what that ambient state is beyond the executing UID, and"
    echo "does not generalize to a differently provisioned account."
} >"$OUT/identity.txt" 2>&1

# --- unentitled normal arm and the checker fault injection, before adding
#     any entitlement, so code-gate validation cannot be pre-empted by
#     entitlement enforcement (REV-047 required change 1) ---
set +e
"$BIN" --label unentitled --scratch-dir "$OUT/scratch" >"$OUT/unentitled.json" 2>"$OUT/unentitled.stderr.txt"
UNENTITLED_RC=$?
set -e
echo "$(decode_rc "$UNENTITLED_RC")" >"$OUT/unentitled.exit.txt"
codesign -d --entitlements - "$BIN" >"$OUT/unentitled.codesign.txt" 2>&1 || true
codesign --verify --verbose "$BIN" >"$OUT/unentitled.verify.txt" 2>&1 || true

set +e
python3 "$SCRIPT_DIR/src/check.py" "$OUT/unentitled.json" >"$OUT/unentitled.check.txt" 2>&1
UNENTITLED_CHECK_RC=$?
set -e

# --- negative control (unentitled binary, --negative flag) ---
set +e
"$BIN" --negative --label negative --scratch-dir "$OUT/scratch" >"$OUT/negative.json" 2>"$OUT/negative.stderr.txt"
NEGATIVE_RC=$?
set -e
echo "$(decode_rc "$NEGATIVE_RC")" >"$OUT/negative.exit.txt"

set +e
python3 "$SCRIPT_DIR/src/check.py" "$OUT/negative.json" >"$OUT/negative.check.txt" 2>&1
NEGATIVE_CHECK_RC=$?
set -e
# Score the negative arm as a demonstrated fault injection only when the
# checker actually parsed a complete output and rejected it on a rule (exit
# 1 with at least one violated rule reported), not merely "nonzero exit" --
# a usage error (exit 2) or an unhandled json.load traceback on truncated
# output must not count as a successful demonstration (REV-048 required
# change 3).
NEGATIVE_DEMONSTRATED=0
if [ "$NEGATIVE_CHECK_RC" -eq 1 ] && grep -q "rule(s) violated" "$OUT/negative.check.txt"; then
    NEGATIVE_DEMONSTRATED=1
fi

# --- entitled-benign arm: a non-restricted entitlement
#     (com.apple.security.get-task-allow) under the same ad-hoc identity, so
#     a termination of the restricted (vmnet) arm below can be attributed to
#     that entitlement's restricted status rather than to ad-hoc signing
#     with any entitlement or to a malformed plist (REV-047 required change 2) ---
set +e
codesign -s - --entitlements "$SCRIPT_DIR/src/benign.entitlements.plist" -f "$BIN" \
    >"$OUT/entitled-benign-sign.stdout.txt" 2>"$OUT/entitled-benign-sign.stderr.txt"
BENIGN_SIGN_RC=$?
set -e
echo "$BENIGN_SIGN_RC" >"$OUT/entitled-benign-sign.exit.txt"
codesign -d --entitlements - "$BIN" >"$OUT/entitled-benign.codesign.txt" 2>&1 || true
codesign --verify --verbose "$BIN" >"$OUT/entitled-benign.verify.txt" 2>&1 || true

BENIGN_RC=125
BENIGN_CHECK_RC=125
if [ "$BENIGN_SIGN_RC" -eq 0 ]; then
    set +e
    "$BIN" --label entitled-benign --scratch-dir "$OUT/scratch" \
        >"$OUT/entitled-benign.json" 2>"$OUT/entitled-benign.stderr.txt"
    BENIGN_RC=$?
    set -e
    if [ "$BENIGN_RC" -eq 0 ]; then
        set +e
        python3 "$SCRIPT_DIR/src/check.py" "$OUT/entitled-benign.json" \
            >"$OUT/entitled-benign.check.txt" 2>&1
        BENIGN_CHECK_RC=$?
        set -e
    else
        echo "checker skipped: process exit $(decode_rc "$BENIGN_RC"); process-level outcome retained" \
            >"$OUT/entitled-benign.check.txt"
    fi
else
    echo "checker skipped: entitlement signing failed" >"$OUT/entitled-benign.check.txt"
fi
echo "$(decode_rc "$BENIGN_RC")" >"$OUT/entitled-benign.exit.txt"

# --- entitled-restricted arm: the actual com.apple.vm.networking entitlement
#     under test. The entitled-benign arm above is the attribution control:
#     if only the restricted arm terminates while the benign arm (same
#     ad-hoc identity, a different entitlement) does not, the termination is
#     attributable to this entitlement's restricted status rather than to
#     ad-hoc signing with any entitlement. ---
set +e
codesign -s - --entitlements "$SCRIPT_DIR/src/vmnet.entitlements.plist" -f "$BIN" \
    >"$OUT/entitled-sign.stdout.txt" 2>"$OUT/entitled-sign.stderr.txt"
SIGN_RC=$?
set -e
echo "$SIGN_RC" >"$OUT/entitled-sign.exit.txt"
codesign -d --entitlements - "$BIN" >"$OUT/entitled-adhoc.codesign.txt" 2>&1 || true
codesign --verify --verbose "$BIN" >"$OUT/entitled-adhoc.verify.txt" 2>&1 || true

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
        echo "checker skipped: process exit $(decode_rc "$ENTITLED_RC"); process-level outcome retained" >"$OUT/entitled-adhoc.check.txt"
    fi
else
    echo "checker skipped: entitlement signing failed" >"$OUT/entitled-adhoc.check.txt"
fi
echo "$(decode_rc "$ENTITLED_RC")" >"$OUT/entitled-adhoc.exit.txt"

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
if [ "$NEGATIVE_DEMONSTRATED" -ne 1 ]; then
    VALIDATION_RC=1
fi
if [ "$BENIGN_SIGN_RC" -ne 0 ]; then
    VALIDATION_RC=1
fi
if [ "$BENIGN_RC" -eq 0 ] && [ "$BENIGN_CHECK_RC" -ne 0 ]; then
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
    echo "negative_demonstrated=$NEGATIVE_DEMONSTRATED"
    echo "entitled_benign_sign_rc=$BENIGN_SIGN_RC"
    echo "entitled_benign_process_rc=$BENIGN_RC"
    echo "entitled_benign_checker_rc=$BENIGN_CHECK_RC"
    echo "entitled_sign_rc=$SIGN_RC"
    echo "entitled_process_rc=$ENTITLED_RC"
    echo "entitled_checker_rc=$ENTITLED_CHECK_RC"
    echo "validation_rc=$VALIDATION_RC"
} >"$OUT/validation.txt"

( cd "$OUT" && find . -type f ! -name SHA256SUMS.txt -exec shasum -a 256 {} \; |
    LC_ALL=C sort -k 2 > SHA256SUMS.txt )

echo "wrote: $OUT" >&2
exit "$VALIDATION_RC"
