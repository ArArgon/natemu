#!/bin/sh
# Independent replication runner for INV-01-12, written for REV-102.
#
# This is NOT experiments/INV-01-12/run.sh. It was written by the replication
# reviewer to execute the approved experiment code at its reviewed revision from
# a clean extraction, with its own sequence, its own verdict logic and its own
# checker (recheck.py). The approved code itself is unmodified: it is taken out
# of git at REV_REVISION with `git archive`, so nothing in the working tree can
# influence what runs.
#
#   NATEMU_RAW_ROOT=/path/outside/repo ./rerun.sh
#
# Writes only under $NATEMU_RAW_ROOT/INV-01-12/replication-<UTC timestamp>/.

set -eu
REV_REVISION=22d8516                 # the revision the code gate accepted
here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../../.." && pwd)

[ "${NATEMU_RAW_ROOT:-}" != "" ] || { echo "refusing: NATEMU_RAW_ROOT unset" >&2; exit 2; }
[ -d "$NATEMU_RAW_ROOT" ] || { echo "refusing: NATEMU_RAW_ROOT is not a directory" >&2; exit 2; }
raw=$(cd "$NATEMU_RAW_ROOT" && pwd -P)
case "$raw/" in "$repo"/*) echo "refusing: raw root is inside the repository" >&2; exit 2 ;; esac

dir="$raw/INV-01-12/replication-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$dir/src" "$dir/build"

# Extract the approved code at its reviewed revision. Nothing from the working
# tree is used, so a dirty checkout cannot silently become the measured object.
git -C "$repo" archive "$REV_REVISION" experiments/INV-01-12 \
  | tar -x -C "$dir/src" --strip-components=2
git -C "$repo" rev-parse "$REV_REVISION" > "$dir/extracted-revision.txt"

{
    echo "=== replication of INV-01-12 / MAN-01-12-01"
    echo "=== reviewer: t01_prototype_reviewer (REV-102); runner and checker written by the reviewer"
    echo "=== date -u";        date -u
    echo "=== approved revision extracted"; cat "$dir/extracted-revision.txt"
    echo "=== repo HEAD at replication"; git -C "$repo" rev-parse HEAD
    echo "=== sw_vers";        sw_vers
    echo "=== sysctl";         sysctl hw.model hw.memsize hw.ncpu hw.pagesize kern.osversion
    echo "=== uptime";         uptime
    echo "=== clang";          clang --version
    echo "=== python3";        python3 --version
    echo "=== extracted source digests"
    shasum -a 256 "$dir/src/prototype/proto.c" "$dir/src/prototype/guest.s" \
                  "$dir/src/src/elfextract.py" "$dir/src/src/check.py" "$dir/src/build.sh"
} > "$dir/env.txt" 2>&1

bin=$("$dir/src/build.sh" "$dir/build")
{ echo "=== codesign entitled";   codesign -d --entitlements - "$bin" 2>&1 || true
  echo "=== codesign unentitled"; codesign -d --entitlements - "$dir/build/natemu_proto_unentitled" 2>&1 || true
} >> "$dir/env.txt"

status=0
note() { echo "$*" | tee -a "$dir/summary.txt"; }

# Positive repetitions: the reviewer's own checker is the verdict, not the
# subject's. The subject checker is run alongside for comparison only.
for i in 1 2 3 4 5; do
    "$bin" --timeout 180 --out "$dir/positive-$i.json" > "$dir/positive-$i.stdout" 2>&1 || true
    if python3 "$here/recheck.py" "$dir/positive-$i.json" > "$dir/positive-$i.recheck" 2>&1
    then note "positive-$i: PASS (reviewer checker)"
    else note "positive-$i: FAIL (reviewer checker)"; status=1; fi
    python3 "$dir/src/src/check.py" "$dir/positive-$i.json" > "$dir/positive-$i.subjectcheck" 2>&1 || true
done

# Controls: each must be rejected by the reviewer's checker. The subject
# checker's --expect is run alongside to confirm the targeted invariants fire.
control() {
    name=$1; targeted=$2; shift 2
    "$bin" --timeout 180 --out "$dir/$name.json" "$@" > "$dir/$name.stdout" 2>&1 || true
    if python3 "$here/recheck.py" "$dir/$name.json" > "$dir/$name.recheck" 2>&1
    then note "$name: NOT REJECTED by the reviewer checker"; status=1
    else note "$name: rejected (reviewer checker)"; fi
    python3 "$dir/src/src/check.py" "$dir/$name.json" ${targeted:+--expect "$targeted"} \
        > "$dir/$name.subjectcheck" 2>&1 || true
    if [ -n "$targeted" ] && grep -q UNWITNESSED "$dir/$name.subjectcheck"; then
        note "  $name: UNWITNESSED targeted invariant"; status=1
    fi
}
control control-push-order    I6d,I6e             --inject-push-order
control control-skip-restart  I5d,I6a,I6b,I6c     --inject-skip-restart
control control-nonatomic-rmw I3                  --inject-nonatomic-rmw
control control-drop-ipi      I11a,I11c           --inject-drop-ipi
control control-stale-cache   I8b,I8c             --inject-stale-cache
control control-no-barrier    ""                  --inject-no-barrier

# ThreadSanitizer and the unentitled arm.
"$dir/build/natemu_proto_tsan" --timeout 600 --out "$dir/tsan.json" > "$dir/tsan.stdout" 2>&1 || true
if grep -q "WARNING: ThreadSanitizer" "$dir/tsan.stdout"
then note "tsan: RACES REPORTED"; status=1
else note "tsan: clean"; fi
"$dir/build/natemu_proto_unentitled" --timeout 180 --out "$dir/unentitled.json" \
    > "$dir/unentitled.stdout" 2>&1 || true
note "unentitled: jit_mapping=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["jit_mapping"])' "$dir/unentitled.json")"

python3 "$here/recheck.py" --selftest | tee -a "$dir/summary.txt"
( cd "$dir" && find . -type f ! -name sha256sums.txt | sort | xargs shasum -a 256 > sha256sums.txt )
note "replication artifacts: $dir"
exit $status
