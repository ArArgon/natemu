#!/bin/sh
# Evidence run for INV-01-01. Refuses to run without NATEMU_RAW_ROOT set to
# an existing directory outside this repository, and writes nothing under
# experiments/ (builds into a mktemp -d directory instead — REV-039 C7).
# See ../../docs/research/evidence-policy.md and the author brief's
# experiment rules for the contract this script must satisfy.
set -eu
cd "$(dirname "$0")"
REPO_ROOT=$(git rev-parse --show-toplevel)

if [ -z "${NATEMU_RAW_ROOT:-}" ]; then
    echo "refusing to run: NATEMU_RAW_ROOT is not set" >&2
    exit 1
fi
if [ ! -d "$NATEMU_RAW_ROOT" ]; then
    echo "refusing to run: NATEMU_RAW_ROOT ($NATEMU_RAW_ROOT) is not an existing directory" >&2
    exit 1
fi
# Resolve before comparing (REV-039 C8): a symlink whose target is inside
# the repository must not pass a string-prefix test on the unresolved path.
RAW_ROOT_RESOLVED=$(cd "$NATEMU_RAW_ROOT" && pwd -P)
REPO_ROOT_RESOLVED=$(cd "$REPO_ROOT" && pwd -P)
case "$RAW_ROOT_RESOLVED" in
    "$REPO_ROOT_RESOLVED"|"$REPO_ROOT_RESOLVED"/*)
        echo "refusing to run: NATEMU_RAW_ROOT must be outside the repository ($REPO_ROOT_RESOLVED)" >&2
        exit 1
        ;;
esac

# run_with_timeout SECS CMD...: POSIX-portable wall-clock timeout (macOS
# ships no `timeout(1)`). Kills CMD if it outlives SECS so a hung probe
# cannot fill the raw root (REV-039 C3 — an unguarded fault used to loop
# forever before the fault-guard fix; this is the backstop, not the fix).
run_with_timeout() {
    secs=$1; shift
    "$@" &
    pid=$!
    ( sleep "$secs"; kill -9 "$pid" 2>/dev/null ) &
    watchdog=$!
    rc=0
    wait "$pid" 2>/dev/null || rc=$?
    kill "$watchdog" 2>/dev/null || true
    wait "$watchdog" 2>/dev/null || true
    return "$rc"
}
TIMEOUT_SECS=30

BUILD_DIR=$(mktemp -d)
trap 'rm -rf "$BUILD_DIR"' EXIT
./build.sh "$BUILD_DIR" >&2

TS=$(date -u +%Y%m%dT%H%M%SZ)
REV=$(git -C "$REPO_ROOT" rev-parse --short HEAD)
RUN_ID="${TS}-${REV}"
OUT="$NATEMU_RAW_ROOT/INV-01-01/$RUN_ID"
mkdir -p "$OUT"

# --- unentitled run (ad-hoc signed, no entitlements) ---
# `|| true`: a timed-out/killed run must not abort the script under `set -e`
# and lose every other run and the environment capture; its output file is
# simply truncated/empty, which check.py and json.load already treat as a
# visible failure, not a silent one.
run_with_timeout "$TIMEOUT_SECS" "$BUILD_DIR/probe" --label unentitled > "$OUT/unentitled.json" || true
codesign -d --entitlements - "$BUILD_DIR/probe" > "$OUT/unentitled.codesign.txt" 2>&1 || true

# --- entitled run (ad-hoc signed with com.apple.security.hypervisor) ---
codesign -s - --entitlements src/hypervisor.entitlements.plist -f "$BUILD_DIR/probe" >&2
run_with_timeout "$TIMEOUT_SECS" "$BUILD_DIR/probe" --label entitled > "$OUT/entitled.json" || true
codesign -d --entitlements - "$BUILD_DIR/probe" > "$OUT/entitled.codesign.txt" 2>&1 || true

# --- negative control (entitled binary, --negative flag) ---
run_with_timeout "$TIMEOUT_SECS" "$BUILD_DIR/probe" --negative --label entitled-negative > "$OUT/negative.json" || true

# --- checker over every output the code gate must see pass/fail correctly ---
python3 src/check.py "$OUT/unentitled.json" > "$OUT/unentitled.check.txt" 2>&1 || true
python3 src/check.py "$OUT/entitled.json" > "$OUT/entitled.check.txt" 2>&1 || true
python3 src/check.py "$OUT/negative.json" > "$OUT/negative.check.txt" 2>&1 || true

# --- positive control (REV-038 M7): the unentitled and entitled runs must
# disagree on hv_vm_create_hr; an identical result indicates a signing or
# entitlement-application defect, not a platform fact. A procedure, not
# just an assertion in prose. ---
POSITIVE_CONTROL_RC=0
python3 - "$OUT/unentitled.json" "$OUT/entitled.json" > "$OUT/positive_control.txt" <<'PYEOF' || POSITIVE_CONTROL_RC=$?
import json, sys
u = json.load(open(sys.argv[1]))["hypervisor"]["hv_vm_create_hr"]
e = json.load(open(sys.argv[2]))["hypervisor"]["hv_vm_create_hr"]
if u == e:
    print(f"FAIL: unentitled and entitled hv_vm_create_hr both {u!r} "
          "(expected them to differ) -- possible signing/entitlement defect")
    sys.exit(1)
print(f"PASS: unentitled={u!r} entitled={e!r}")
PYEOF

# --- exclusion criterion support: kern.hv_support is captured in host.* of
# every run; a run whose entitled kern_hv_support reads 0 is EXCLUDED from
# the KA-02-adjacent Hypervisor result (hardware support absent is a
# different alternative than "no entitlement") and must be rerun on
# hardware that reports support, per INV-01-01.md Exclusion criteria. This
# script does not auto-exclude; a result-gate reviewer applies the rule
# using the recorded field. ---

# --- environment capture ---
{
    echo "== sw_vers =="
    sw_vers
    echo "== sysctl =="
    sysctl hw.model hw.memsize hw.ncpu hw.pagesize kern.osversion kern.hv_support
    echo "== clang --version =="
    clang --version
    echo "== git rev-parse HEAD =="
    git -C "$REPO_ROOT" rev-parse HEAD
    echo "== resolved SDK path (readlink) =="
    xcrun --show-sdk-path
    readlink -f "$(xcrun --show-sdk-path)" 2>/dev/null || true
    echo "== SHA-256 of the SDK headers INV-01-01's source inventory cites =="
    SDKROOT="$(xcrun --show-sdk-path)"
    for h in \
        usr/include/pthread.h \
        usr/include/sys/mman.h \
        usr/include/mach/mach_vm.h \
        usr/include/arm64/hv/hv_kern_types.h \
        "System/Library/Frameworks/Hypervisor.framework/Versions/A/Headers/hv_vm.h" \
        "System/Library/Frameworks/Hypervisor.framework/Versions/A/Headers/hv_vcpu.h" \
        "System/Library/Frameworks/Hypervisor.framework/Versions/A/Headers/hv_vcpu_types.h" \
        "System/Library/Frameworks/Hypervisor.framework/Versions/A/Headers/Hypervisor.h"; do
        if [ -f "$SDKROOT/$h" ]; then
            shasum -a 256 "$SDKROOT/$h"
        else
            echo "MISSING: $SDKROOT/$h"
        fi
    done
} > "$OUT/environment.txt" 2>&1

# --- SHA-256 of every raw file, for the manifest ---
( cd "$OUT" && shasum -a 256 -- * > SHA256SUMS.txt )

if [ "$POSITIVE_CONTROL_RC" -ne 0 ]; then
    echo "wrote: $OUT (WARNING: positive control failed, see positive_control.txt)" >&2
else
    echo "wrote: $OUT" >&2
fi
