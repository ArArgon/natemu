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
    name=$1; expect=$2; targeted=$3; shift 3
    if "$bin" --timeout 180 --out "$dir/$name.json" "$@" > "$dir/$name.stdout" 2>&1
    then rc=0; else rc=$?; fi
    # A host-level death (signal) writes no JSON at all. Synthesize an abort
    # document so the checker reports a named invariant failure rather than a
    # traceback, and so the raw artifact records what happened.
    if [ ! -s "$dir/$name.json" ]; then
        printf '{"record":"INV-01-12","injection":"unknown","jit_mapping":"unknown",\n' \
            > "$dir/$name.json"
        printf '"timed_out":false,"abort":"process exited %s without writing output",\n' "$rc" \
            >> "$dir/$name.json"
        printf '"host":{},"vcpu":[],"guest":{"fault_log":[]}}\n' >> "$dir/$name.json"
    fi
    if python3 "$here/src/check.py" "$dir/$name.json" ${targeted:+--expect "$targeted"} \
        > "$dir/$name.check" 2>&1; then
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
for i in 1 2 3 4 5; do run_one "positive-$i" PASS ""; done

# Each control names the invariants it must break. Anything else it breaks is
# collateral and is not a witness; check.py labels the targeted ones and fails
# the run if a targeted invariant did not fire.
run_one control-push-order    FAIL I6d,I6e     --inject-push-order
run_one control-skip-restart  FAIL I5d,I6a,I6b,I6c --inject-skip-restart
run_one control-nonatomic-rmw FAIL I3          --inject-nonatomic-rmw
run_one control-drop-ipi      FAIL I11a,I11c   --inject-drop-ipi
run_one control-stale-cache   FAIL I8b,I8c     --inject-stale-cache
# The no-barrier control is stochastic by predeclaration: it does not carry the
# code gate, and its verdict is recorded either way.
run_one control-no-barrier    ""   ""          --inject-no-barrier

# ThreadSanitizer run: recorded, not gating. Any race report is a code-gate
# failure, so the output is kept alongside the invariant verdicts.
"$dir/build/natemu_proto_tsan" --timeout 600 --out "$dir/tsan.json" \
    > "$dir/tsan.stdout" 2>&1 || true
if grep -q "WARNING: ThreadSanitizer" "$dir/tsan.stdout"; then
    echo "tsan: RACES REPORTED" | tee -a "$dir/summary.txt"
    status=1
else
    echo "tsan: clean" | tee -a "$dir/summary.txt"
fi

# Also record the unentitled outcome for the MAP_JIT dependency (KA-01).
"$dir/build/natemu_proto_unentitled" --timeout 180 --out "$dir/unentitled.json" \
    > "$dir/unentitled.stdout" 2>&1 || true

( cd "$dir" && shasum -a 256 ./*.json ./*.txt ./*.check ./*.stdout > sha256sums.txt )
echo "raw artifacts: $dir"
exit $status
