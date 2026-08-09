#!/bin/bash
# Sweep 3: order-dependence / isolation check.
# ptr8 and ls4tlb showed high run-to-run variance inside the full suite.
# Running them ALONE tests whether the variance comes from QEMU state carried
# over from earlier workloads (software-TLB sizing, TB cache occupancy).
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RES="$HERE/results"; mkdir -p "$RES"
for w in ptr8 ls4tlb ls4 alu4; do
  for r in 1 2 3 4 5; do
    "$HERE/runvm.py" --arch x86_64 --accel tcg --task "bench:$w" --quiet --timeout 3000 \
      --out "$RES/solo.$w.$r.json" >/dev/null 2>&1
  done
done
echo "SWEEP3 DONE"
