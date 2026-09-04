#!/bin/sh
# Evidence run for INV-01-08. Writes nothing under experiments/.
#
#   NATEMU_RAW_ROOT=/path/outside/repo ./run.sh [--negative]
#
# Output goes to $NATEMU_RAW_ROOT/INV-01-08/<UTC timestamp>-<git short rev>/.
set -eu

script_dir=$(cd "$(dirname "$0")" && pwd)
# Ask git for the work tree rather than assuming the script sits two levels
# below it: a relocated copy would otherwise silently change what counts as
# "inside the repository" (REV-045 required change 9). Guard the call so an
# invocation outside a work tree gives the documented exit-2 refusal instead
# of git's own exit 128 under `set -eu`.
if ! repo_root=$(git -C "$script_dir" rev-parse --show-toplevel 2>/dev/null); then
	echo "refusing to run: '$script_dir' is not inside a git work tree, so the raw-root containment check cannot be made" >&2
	exit 2
fi

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
if [ "$#" -gt 1 ] || { [ "$#" -eq 1 ] && [ "$1" != "--negative" ]; }; then
	echo "usage: $0 [--negative]" >&2
	exit 2
fi
if [ "${1:-}" = "--negative" ]; then
	mode=negative
	extra="--negative"
fi

dirty_detail=$(git -C "$repo_root" status --porcelain -- \
	experiments/INV-01-08 docs/research/investigations/INV-01-08.md)
if [ -n "$dirty_detail" ]; then
	echo "refusing evidence run: commit the INV-01-08 method and code first" >&2
	echo "$dirty_detail" >&2
	exit 2
fi
if [ "$(sysctl -n hw.optional.arm.FEAT_LSE 2>/dev/null || echo 0)" != "1" ]; then
	echo "refusing to run: hw.optional.arm.FEAT_LSE is unavailable" >&2
	exit 2
fi

rev=$(git -C "$repo_root" rev-parse --short HEAD)
run_id="$(date -u +%Y%m%dT%H%M%SZ)-$rev"
out_dir="$raw_root/INV-01-08/$run_id"
if [ -e "$out_dir" ]; then
	echo "refusing to overwrite existing run directory: $out_dir" >&2
	exit 2
fi
mkdir -p "$out_dir"

# Build outside the repository so nothing is written under experiments/.
build_dir="$out_dir/build"
make -C "$(dirname "$0")" BUILD="$build_dir" >"$out_dir/build.log" 2>&1
bin="$build_dir/litmus"

{
	echo "run_id=$run_id"
	echo "mode=$mode"
	echo "git_head=$(git -C "$repo_root" rev-parse HEAD)"
	echo "experiment_tree_dirty_files=0"
	echo "---- sw_vers ----"; sw_vers
	echo "---- sysctl ----"; sysctl hw.model hw.memsize hw.ncpu hw.pagesize kern.osversion \
		hw.perflevel0.logicalcpu hw.perflevel1.logicalcpu \
		hw.optional.arm.FEAT_LSE hw.optional.arm.FEAT_LSE2 2>&1
	echo "---- clang ----"; clang --version
	echo "---- sdk ----"
	sdk=$(xcrun --show-sdk-path)
	echo "xcrun --show-sdk-path: $sdk"
	# The path above is a symlink (MacOSX.sdk); the source inventories pin
	# header digests to MacOSX26.4.sdk, so record the resolved target and the
	# digests themselves (REV-045 required change 13).
	echo "resolved: $(cd "$sdk" && pwd -P)"
	xcrun --show-sdk-version
	echo "---- cited header digests ----"
	for h in usr/include/mach/mach_time.h usr/include/mach/thread_policy.h \
		usr/include/pthread/qos.h; do
		[ -f "$sdk/$h" ] && shasum -a 256 "$sdk/$h"
	done
	echo "---- codesign ----"; codesign -d --entitlements - "$bin" 2>&1 || true
	echo "---- uptime/load ----"; uptime
	echo "---- thermal/power ----"; pmset -g therm 2>&1 || true; pmset -g batt 2>&1 || true
} >"$out_dir/environment.txt" 2>&1

"$bin" $extra --iters "${NATEMU_ITERS:-1000000}" --out "$out_dir/result.json"

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
