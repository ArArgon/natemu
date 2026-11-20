#!/bin/sh
# Floating-point check on this Mac (for the owner's M1/M2 runs, accurate FP mode plan chunk 6). From a checkout with the submodule
# initialised (README "Setup"):
#   1. tools/fp_trap_probe/run.sh: trapped FP exceptions, precise and inside a VM (the startup check requires them);
#   2. the test framework's floating-point and fault families (tests/fp, tests/faults), fast and accurate mode, on this host as
#      it is: on a Mac without FEAT_AFP (M1, M2) fast mode passes with the documented deviations, accurate mode exactly; on an
#      M4 both pass exactly. The variants that need another Mac are reported as not runnable.
# Send back the whole report ($TMPDIR/fp_check/report.txt) and the run folder it names.
set -u
here=$(cd "$(dirname "$0")" && pwd); root=$(cd "$here/../.." && pwd)
out=${TMPDIR:-/tmp}/fp_check; mkdir -p "$out"; report=$out/report.txt; : > "$report"
say() { echo "$@" | tee -a "$report"; }
cd "$root"
say "== host: $(sw_vers -productVersion), $(sysctl -n machdep.cpu.brand_string), FEAT_AFP $(sysctl -n hw.optional.arm.FEAT_AFP 2>/dev/null || echo absent), commit $(git rev-parse --short HEAD)"
say "== trap probe"; sh tools/fp_trap_probe/run.sh 2>&1 | tee -a "$report"
say "== tests/fp tests/faults"
tools/natemu-test run tests/fp tests/faults 2>&1 | tail -40 | tee -a "$report"
say "== done: $report"
