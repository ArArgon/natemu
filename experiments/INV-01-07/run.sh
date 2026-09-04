#!/bin/sh
# Evidence run for INV-01-07. Writes nothing under experiments/.
#
#   NATEMU_RAW_ROOT=/path/outside/repo ./run.sh [--negative]
#
# Output goes to $NATEMU_RAW_ROOT/INV-01-07/<UTC timestamp>-<git short rev>/.
set -eu

repo_root=$(cd "$(dirname "$0")/../.." && pwd)

if [ -z "${NATEMU_RAW_ROOT:-}" ]; then
	echo "refusing to run: NATEMU_RAW_ROOT is not set" >&2
	exit 2
fi
if [ ! -d "$NATEMU_RAW_ROOT" ]; then
	echo "refusing to run: NATEMU_RAW_ROOT '$NATEMU_RAW_ROOT' is not an existing directory" >&2
	exit 2
fi
raw_root=$(cd "$NATEMU_RAW_ROOT" && pwd)
case "$raw_root/" in
	"$repo_root"/*)
		echo "refusing to run: NATEMU_RAW_ROOT '$raw_root' is inside the repository '$repo_root'" >&2
		exit 2
		;;
esac

mode=normal
extra=""
if [ "${1:-}" = "--negative" ]; then
	mode=negative
	extra="--negative"
fi

rev=$(git -C "$repo_root" rev-parse --short HEAD)
dirty=$(git -C "$repo_root" status --porcelain -- experiments/INV-01-07 | wc -l | tr -d ' ')
run_id="$(date -u +%Y%m%dT%H%M%SZ)-$rev"
out_dir="$raw_root/INV-01-07/$run_id"
if [ -e "$out_dir" ]; then
	echo "refusing to overwrite existing run directory: $out_dir" >&2
	exit 2
fi
mkdir -p "$out_dir"

# Build outside the repository so nothing is written under experiments/.
build_dir="$out_dir/build"
make -C "$(dirname "$0")" BUILD="$build_dir" >"$out_dir/build.log" 2>&1
bin="$build_dir/timers"

{
	echo "run_id=$run_id"
	echo "mode=$mode"
	echo "git_head=$(git -C "$repo_root" rev-parse HEAD)"
	echo "experiment_tree_dirty_files=$dirty"
	echo "---- sw_vers ----"; sw_vers
	echo "---- sysctl ----"; sysctl hw.model hw.memsize hw.ncpu hw.pagesize kern.osversion \
		hw.perflevel0.logicalcpu hw.perflevel1.logicalcpu \
		hw.optional.arm.FEAT_LSE hw.optional.arm.FEAT_LSE2 2>&1
	echo "---- clang ----"; clang --version
	echo "---- sdk ----"; xcrun --show-sdk-path; xcrun --show-sdk-version
	echo "---- codesign ----"; codesign -d --entitlements - "$bin" 2>&1 || true
	echo "---- uptime/load ----"; uptime
	echo "---- thermal/power ----"; pmset -g therm 2>&1 || true; pmset -g batt 2>&1 || true
} >"$out_dir/environment.txt" 2>&1

"$bin" $extra --n "${NATEMU_N:-2000}" --out "$out_dir/result.json"

# Post-run environment, so thermal drift across the run is visible.
{ echo "---- after ----"; uptime; pmset -g therm 2>&1 || true; } >>"$out_dir/environment.txt" 2>&1

set +e
python3 "$(dirname "$0")/src/check.py" "$out_dir/result.json" >"$out_dir/check.txt" 2>&1
rc=$?
set -e
cat "$out_dir/check.txt"

( cd "$out_dir" && shasum -a 256 result.json environment.txt check.txt build.log > sha256sums.txt )
echo "raw artifacts: $out_dir"
exit $rc
