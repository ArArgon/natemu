#!/bin/sh
# Floating-point check on this Mac (for the owner's M1/M2 runs, accurate FP mode plan chunk 6). From a checkout with the submodule
# initialised (README "Setup"), builds natemu and the x86 compiler into $TMPDIR/fp_check (nothing installed), then:
#   1. tools/fp_trap_probe/run.sh: trapped FP exceptions, precise and inside a VM (the startup check requires them);
#   2. the four FP runners in fast mode and in accurate mode (--fp via NATEMU_FP), on this host as it is (no emulation).
# On a Mac without FEAT_AFP (M1, M2) fast mode passes with the documented deviations counted ("deviated as documented") and
# accurate mode passes with none; on an M4 both pass exactly. Send back the whole report ($TMPDIR/fp_check/report.txt).
set -u
here=$(cd "$(dirname "$0")" && pwd); root=$(cd "$here/../.." && pwd)
out=${TMPDIR:-/tmp}/fp_check; mkdir -p "$out"; report=$out/report.txt; : > "$report"
say() { echo "$@" | tee -a "$report"; }
cd "$root"
say "== host: $(sw_vers -productVersion), $(sysctl -n machdep.cpu.brand_string), FEAT_AFP $(sysctl -n hw.optional.arm.FEAT_AFP 2>/dev/null || echo absent), commit $(git rev-parse --short HEAD)"
say "== trap probe"; sh tools/fp_trap_probe/run.sh 2>&1 | tee -a "$report"
say "== build"
{ cargo build --release -p natemu-cli --bin natemu && cargo build --release -p natemu-runtime --target aarch64-unknown-none &&
  codesign --force --sign - --entitlements crates/host/entitlements.plist target/release/natemu; } > "$out/build.log" 2>&1 || { say "natemu build failed: $out/build.log"; exit 1; }
fexc=$out/fexc/obj/fexc
[ -x "$fexc" ] || { rm -rf "$out/fexc"; tools/fexc/build.sh "$out/fexc" > "$out/fexc-build.log" 2>&1 || { say "fexc build failed: $out/fexc-build.log"; exit 1; }; }
say "fexc $(shasum -a 256 "$fexc" | cut -c1-16)"
for mode in fast accurate; do
  for r in run_i5c run_i5a_de run_i6_faults run_i8_faults; do
    NATEMU_FP=$mode tools/with-env python3 tools/$r.py --fexc "$fexc" --out "$out/$r-$mode" > "$out/$r-$mode.log" 2>&1; rc=$?
    say "$mode $r rc=$rc: $(tail -1 "$out/$r-$mode.log")"
    grep -hE '^(FAIL|MISMATCH)' "$out/$r-$mode.log" | head -20 | tee -a "$report"
  done
done
say "== done: $report"
