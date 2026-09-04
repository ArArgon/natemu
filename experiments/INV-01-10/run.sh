#!/bin/sh
# Evidence run for INV-01-10. Refuses to run without NATEMU_RAW_ROOT set to
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

chmod +x src/tools.sh

TS=$(date -u +%Y%m%dT%H%M%SZ)
REV=$(git -C "$REPO_ROOT" rev-parse --short HEAD)
RUN_ID="${TS}-${REV}"
OUT="$NATEMU_RAW_ROOT/INV-01-10/$RUN_ID"
mkdir -p "$OUT"

# --- positive run ---
src/tools.sh > "$OUT/tools.json"

# --- negative control ---
src/tools.sh --negative > "$OUT/negative.json"

# --- checker over both ---
python3 src/check.py "$OUT/tools.json" > "$OUT/tools.check.txt" 2>&1 || true
python3 src/check.py "$OUT/negative.json" > "$OUT/negative.check.txt" 2>&1 || true

# --- environment capture ---
{
    echo "== sw_vers =="
    sw_vers
    echo "== git rev-parse HEAD =="
    git -C "$REPO_ROOT" rev-parse HEAD
    echo "== xcode-select -p =="
    xcode-select -p
} > "$OUT/environment.txt" 2>&1

# --- SHA-256 of every raw file, for the manifest ---
( cd "$OUT" && shasum -a 256 -- * > SHA256SUMS.txt )

echo "wrote: $OUT" >&2
