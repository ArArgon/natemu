#!/bin/sh
# Builds the probe (ad-hoc signed with the hypervisor entitlement) and prints the vCPU's Arm features against sysctl.
set -e
here=$(cd "$(dirname "$0")" && pwd)
out=${TMPDIR:-/tmp}/hvf_id_probe
clang -O1 -o "$out" "$here/probe.c" -framework Hypervisor
codesign -s - --entitlements "$here/ent.plist" -f "$out" 2>/dev/null
sw_vers -productVersion
sysctl -n machdep.cpu.brand_string
"$out" | python3 "$here/decode.py"
