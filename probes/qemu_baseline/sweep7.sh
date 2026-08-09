#!/bin/bash
# Sweep 7: a real kernel-heavy workload -- 400 x fork+exec of a static binary
# in the guest. This is the workload that stresses guest page-table teardown,
# TLB flushes, and translation of freshly-mapped code, none of which the
# compute microbenchmarks touch.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RES="$HERE/results"; mkdir -p "$RES"
for r in 1 2 3; do
  "$HERE/runvm.py" --arch x86_64  --accel tcg --task spawn --quiet --timeout 3000 --out "$RES/spawn.x86_64-tcg.$r.json"  >/dev/null 2>&1
  "$HERE/runvm.py" --arch aarch64 --accel hvf --task spawn --quiet --timeout 3000 --out "$RES/spawn.aarch64-hvf.$r.json" >/dev/null 2>&1
  "$HERE/runvm.py" --arch aarch64 --accel tcg --task spawn --quiet --timeout 3000 --out "$RES/spawn.aarch64-tcg.$r.json" >/dev/null 2>&1
done
echo "SWEEP7 DONE"
