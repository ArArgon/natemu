#!/bin/sh
# Evidence run for INV-01-01. Refuses to run without NATEMU_RAW_ROOT set to
# an existing directory outside this repository, and writes nothing under
# experiments/. See ../../docs/research/evidence-policy.md and the author
# brief's experiment rules for the contract this script must satisfy.
set -eu
cd "$(dirname "$0")"
REPO_ROOT=$(cd ../.. && pwd)

if [ -z "${NATEMU_RAW_ROOT:-}" ]; then
    echo "refusing to run: NATEMU_RAW_ROOT is not set" >&2
    exit 1
fi
if [ ! -d "$NATEMU_RAW_ROOT" ]; then
    echo "refusing to run: NATEMU_RAW_ROOT ($NATEMU_RAW_ROOT) is not an existing directory" >&2
    exit 1
fi
case "$NATEMU_RAW_ROOT" in
    "$REPO_ROOT"|"$REPO_ROOT"/*)
        echo "refusing to run: NATEMU_RAW_ROOT must be outside the repository ($REPO_ROOT)" >&2
        exit 1
        ;;
esac

./build.sh >&2

TS=$(date -u +%Y%m%dT%H%M%SZ)
REV=$(git -C "$REPO_ROOT" rev-parse --short HEAD)
RUN_ID="${TS}-${REV}"
OUT="$NATEMU_RAW_ROOT/INV-01-01/$RUN_ID"
mkdir -p "$OUT"

# --- unentitled run (ad-hoc signed, no entitlements) ---
build/probe --label unentitled > "$OUT/unentitled.json"
codesign -d --entitlements - build/probe > "$OUT/unentitled.codesign.txt" 2>&1 || true

# --- entitled run (ad-hoc signed with com.apple.security.hypervisor) ---
codesign -s - --entitlements src/hypervisor.entitlements.plist -f build/probe >&2
build/probe --label entitled > "$OUT/entitled.json"
codesign -d --entitlements - build/probe > "$OUT/entitled.codesign.txt" 2>&1 || true

# --- negative control (entitled binary, --negative flag) ---
build/probe --negative --label entitled-negative > "$OUT/negative.json"

# --- checker over every output the code gate must see pass/fail correctly ---
python3 src/check.py "$OUT/unentitled.json" > "$OUT/unentitled.check.txt" 2>&1 || true
python3 src/check.py "$OUT/entitled.json" > "$OUT/entitled.check.txt" 2>&1 || true
python3 src/check.py "$OUT/negative.json" > "$OUT/negative.check.txt" 2>&1 || true

# --- environment capture ---
{
    echo "== sw_vers =="
    sw_vers
    echo "== sysctl =="
    sysctl hw.model hw.memsize hw.ncpu hw.pagesize kern.osversion
    echo "== clang --version =="
    clang --version
    echo "== git rev-parse HEAD =="
    git -C "$REPO_ROOT" rev-parse HEAD
    echo "== xcrun --show-sdk-path =="
    xcrun --show-sdk-path
} > "$OUT/environment.txt" 2>&1

# --- SHA-256 of every raw file, for the manifest ---
( cd "$OUT" && shasum -a 256 -- * > SHA256SUMS.txt )

echo "wrote: $OUT" >&2
