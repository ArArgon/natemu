#!/bin/sh
# Evidence run for INV-01-10. Refuses to run without NATEMU_RAW_ROOT set to
# an existing directory outside this repository, and writes nothing under
# experiments/. See ../../docs/research/evidence-policy.md and the author
# brief's experiment rules for the contract this script must satisfy.
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
# Resolve before comparing (REV-054 C5, same fix as REV-039 C8 in
# INV-01-01/run.sh): a symlink whose target is inside the repository must
# not pass a string-prefix test on the unresolved path.
RAW_ROOT_RESOLVED=$(cd "$NATEMU_RAW_ROOT" && pwd -P)
REPO_ROOT_RESOLVED=$(cd "$REPO_ROOT" && pwd -P)
case "$RAW_ROOT_RESOLVED" in
    "$REPO_ROOT_RESOLVED"|"$REPO_ROOT_RESOLVED"/*)
        echo "refusing to run: NATEMU_RAW_ROOT must be outside the repository ($REPO_ROOT_RESOLVED)" >&2
        exit 1
        ;;
esac

TS=$(date -u +%Y%m%dT%H%M%SZ)
REV=$(git -C "$REPO_ROOT" rev-parse --short HEAD)
RUN_ID="${TS}-${REV}"
OUT="$NATEMU_RAW_ROOT/INV-01-10/$RUN_ID"
mkdir -p "$OUT"

# --- positive run --- (invoke via the interpreter, not `./tools.sh`, so
# this script never needs to chmod +x anything under experiments/ —
# REV-054 C4; the file is already executable in the repository, but a
# script that asserts it writes nothing under experiments/ should not
# contain a write to experiments/ even a redundant one)
sh src/tools.sh > "$OUT/tools.json"

# --- negative control ---
sh src/tools.sh --negative > "$OUT/negative.json"

# --- checker over both ---
python3 src/check.py "$OUT/tools.json" > "$OUT/tools.check.txt" 2>&1 || true
python3 src/check.py "$OUT/negative.json" > "$OUT/negative.check.txt" 2>&1 || true

# --- environment capture --- (REV-094 R1: host identity was missing —
# Mac16,10/hw.memsize/hw.ncpu/hw.pagesize appeared in none of the six
# artifact files, so a result reviewer holding only this artifact could
# not confirm the host the applicability statement names. Added the same
# sysctl/clang block INV-01-01's run.sh already captures.)
{
    echo "== sw_vers =="
    sw_vers
    echo "== sysctl =="
    sysctl hw.model hw.memsize hw.ncpu hw.pagesize kern.osversion
    echo "== clang --version =="
    clang --version
    echo "== git rev-parse HEAD =="
    git -C "$REPO_ROOT" rev-parse HEAD
    echo "== xcode-select -p =="
    xcode-select -p
} > "$OUT/environment.txt" 2>&1

# --- SHA-256 of every raw file, for the manifest ---
( cd "$OUT" && shasum -a 256 -- * > SHA256SUMS.txt )

echo "wrote: $OUT" >&2
