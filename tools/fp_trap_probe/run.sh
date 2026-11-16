#!/bin/sh
# Does this Mac implement trapped floating-point exceptions, precisely, and do they reach a guest's own EL1 under
# Hypervisor.framework? Accurate FP mode's design depends on it (docs/restructure/afp-accurate-plan.md). Builds three small
# programs into $TMPDIR (nothing installed; the HVF one is ad-hoc signed with the hypervisor entitlement) and prints:
#   - the OS's FP_SyncExceptions bit;
#   - trap_test / trap_vec: user-mode traps per FPCR enable (scalar and vector), the ESR, and whether the destination is unchanged;
#   - hvf_trap: traps inside a VM (EL0 code, EL1 vector), ns per trap, ESR, destination unchanged.
# Send the whole output back. On an M4 every trap case reports EC 0x2c with the destination unchanged.
here=$(cd "$(dirname "$0")" && pwd)
out=${TMPDIR:-/tmp}/fp_trap_probe; mkdir -p "$out"
sw_vers -productVersion
sysctl -n machdep.cpu.brand_string
echo "FP_SyncExceptions: $(sysctl -n hw.optional.arm.FP_SyncExceptions 2>/dev/null || echo absent)"
for p in trap_test trap_vec; do
  clang -O1 -o "$out/$p" "$here/$p.c" && { echo "== $p"; "$out/$p"; }
done
clang -O2 -o "$out/hvf_trap" "$here/hvf_trap.c" -framework Hypervisor &&
  codesign -s - --entitlements "$here/ent.plist" -f "$out/hvf_trap" 2>/dev/null &&
  { echo "== hvf_trap"; "$out/hvf_trap"; }
