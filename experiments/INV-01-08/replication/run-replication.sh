#!/bin/sh
# Independent replication runner for INV-01-08, written by reviewer context
# `t01_events_reviewer` for REV-086. It is deliberately NOT the author's
# run.sh: it extracts the approved experiment code from Git at a pinned
# revision with `git archive` (so nothing in the working tree can influence
# the build), builds it outside the repository, runs it at full counts, and
# checks the output with this directory's independent checker.
#
#   NATEMU_RAW_ROOT=/path/outside/repo ./run-replication.sh [<revision>]
#
# Output: $NATEMU_RAW_ROOT/INV-01-08/replication-<UTC>/
set -eu

rev=${1:-d8cb40c}
here=$(cd "$(dirname "$0")" && pwd)
repo=$(git -C "$here" rev-parse --show-toplevel 2>/dev/null) || {
	echo "refusing to run: not inside a git work tree" >&2; exit 2; }

[ -n "${NATEMU_RAW_ROOT:-}" ] || { echo "refusing to run: NATEMU_RAW_ROOT is not set" >&2; exit 2; }
[ -d "$NATEMU_RAW_ROOT" ] || { echo "refusing to run: NATEMU_RAW_ROOT is not an existing directory" >&2; exit 2; }
raw=$(cd "$NATEMU_RAW_ROOT" && pwd -P)
case "$raw/" in "$repo"/*) echo "refusing to run: raw root is inside the repository" >&2; exit 2;; esac

out="$raw/INV-01-08/replication-$(date -u +%Y%m%dT%H%M%SZ)"
[ -e "$out" ] && { echo "refusing to overwrite $out" >&2; exit 2; }
mkdir -p "$out"

# Pinned source, extracted from Git rather than read from the working tree.
src=$(mktemp -d "${TMPDIR:-/tmp}/inv0108-repl.XXXXXX")
trap 'rm -rf "$src"' EXIT HUP INT TERM
git -C "$repo" archive "$rev" experiments/INV-01-08 | tar -x -C "$src"
tree="$src/experiments/INV-01-08"
( cd "$tree" && find src Makefile -type f | sort | xargs shasum -a 256 ) > "$out/source-digests.txt"

make -C "$tree" BUILD="$out/build" >"$out/build.log" 2>&1
bin="$out/build/litmus"

{
	echo "replication_of=INV-01-08"
	echo "experiment_revision=$(git -C "$repo" rev-parse "$rev")"
	echo "reviewer_context=t01_events_reviewer"
	echo "iters=${NATEMU_ITERS:-1000000}"
	echo "---- sw_vers ----"; sw_vers
	echo "---- sysctl ----"; sysctl hw.model hw.memsize hw.ncpu hw.pagesize kern.osversion \
		hw.perflevel0.logicalcpu hw.perflevel1.logicalcpu \
		hw.optional.arm.FEAT_LSE hw.optional.arm.FEAT_LSE2 2>&1
	echo "---- clang ----"; clang --version
	echo "---- sdk ----"; echo "xcrun --show-sdk-path: $(xcrun --show-sdk-path)"
	echo "resolved: $(cd "$(xcrun --show-sdk-path)" && pwd -P)"; xcrun --show-sdk-version
	echo "---- uptime/load ----"; uptime
	echo "---- thermal/power ----"; pmset -g therm 2>&1 || true; pmset -g batt 2>&1 || true
} >"$out/environment.txt" 2>&1

"$bin" --iters "${NATEMU_ITERS:-1000000}" --out "$out/result.json"

{ echo "---- after ----"; uptime; pmset -g therm 2>&1 || true; } >>"$out/environment.txt" 2>&1

set +e
python3 "$here/verify.py" "$out/result.json" >"$out/verify.txt" 2>&1
rc=$?
python3 "$tree/src/check.py" "$out/result.json" >"$out/check.txt" 2>&1
arc=$?
set -e
cat "$out/verify.txt"
echo "author checker exit=$arc (recorded in check.txt)"

( cd "$out" && shasum -a 256 result.json environment.txt verify.txt check.txt build.log source-digests.txt > sha256sums.txt )
echo "raw artifacts: $out"
exit $rc
