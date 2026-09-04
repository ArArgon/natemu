#!/bin/sh
# Evidence run for INV-01-12.
#
# Writes only under $NATEMU_RAW_ROOT/INV-01-12/<run-id>/ and never under
# experiments/. <run-id> is <UTC timestamp>-<git short rev>.
#
#   NATEMU_RAW_ROOT=/path/outside/repo ./run.sh
#
# Runs the positive integrated run and every negative control, and evaluates
# each with src/check.py. The positive run must PASS; every control must FAIL.

set -eu
here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../.." && pwd)

if [ "${NATEMU_RAW_ROOT:-}" = "" ]; then
    echo "refusing to run: NATEMU_RAW_ROOT is not set" >&2
    exit 2
fi
if [ ! -d "$NATEMU_RAW_ROOT" ]; then
    echo "refusing to run: NATEMU_RAW_ROOT ($NATEMU_RAW_ROOT) is not an existing directory" >&2
    exit 2
fi
raw=$(cd "$NATEMU_RAW_ROOT" && pwd -P)
case "$raw/" in
    "$repo"/*) echo "refusing to run: NATEMU_RAW_ROOT lies inside the repository" >&2; exit 2 ;;
esac

rev=$(git -C "$repo" rev-parse --short HEAD)
runid=$(date -u +%Y%m%dT%H%M%SZ)-$rev
dir="$raw/INV-01-12/$runid"
mkdir -p "$dir/build"

bin=$("$here/build.sh" "$dir/build")

{
    echo "=== run-id"; echo "$runid"
    echo "=== date -u"; date -u
    echo "=== sw_vers"; sw_vers
    echo "=== sysctl"; sysctl hw.model hw.memsize hw.ncpu hw.pagesize kern.osversion
    echo "=== clang --version"; clang --version
    echo "=== python3 --version"; python3 --version
    echo "=== git rev-parse HEAD"; git -C "$repo" rev-parse HEAD
    echo "=== git status --porcelain"; git -C "$repo" status --porcelain
    echo "=== codesign entitled binary"; codesign -d --entitlements - "$bin" 2>&1 || true
    echo "=== codesign unentitled binary"; codesign -d --entitlements - "$dir/build/natemu_proto_unentitled" 2>&1 || true
    echo "=== shasum of inputs"
    shasum -a 256 "$here/prototype/proto.c" "$here/prototype/guest.s" \
                  "$here/src/elfextract.py" "$here/src/check.py" \
                  "$dir/build/guest_blob.h" "$bin"
} > "$dir/env.txt" 2>&1

status=0
run_one() {
    name=$1; expect=$2; shift 2
    "$bin" --timeout 180 --out "$dir/$name.json" "$@" > "$dir/$name.stdout" 2>&1 || true
    if python3 "$here/src/check.py" "$dir/$name.json" > "$dir/$name.check" 2>&1; then
        verdict=PASS
    else
        verdict=FAIL
    fi
    echo "$name: $verdict (expected ${expect:-any})" | tee -a "$dir/summary.txt"
    if [ -n "$expect" ] && [ "$verdict" != "$expect" ]; then
        echo "  UNEXPECTED verdict for $name" >&2
        status=1
    fi
}

python3 "$here/src/check.py" --selftest | tee -a "$dir/summary.txt"

# Five repetitions of the positive run; all five must PASS. A flaky integrated
# prototype is a failure, not a censored sample.
for i in 1 2 3 4 5; do run_one "positive-$i" PASS; done

run_one control-skip-restart  FAIL --inject-skip-restart
run_one control-nonatomic-rmw FAIL --inject-nonatomic-rmw
run_one control-drop-ipi      FAIL --inject-drop-ipi
run_one control-stale-cache   FAIL --inject-stale-cache
# The no-barrier control is stochastic by predeclaration: it does not carry the
# code gate, and its verdict is recorded either way.
run_one control-no-barrier    ""   --inject-no-barrier

# Also record the unentitled outcome for the MAP_JIT dependency (KA-01).
"$dir/build/natemu_proto_unentitled" --timeout 180 --out "$dir/unentitled.json" \
    > "$dir/unentitled.stdout" 2>&1 || true

( cd "$dir" && shasum -a 256 ./*.json ./*.txt ./*.check ./*.stdout > sha256sums.txt )
echo "raw artifacts: $dir"
exit $status
