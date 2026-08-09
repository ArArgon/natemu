#!/bin/bash
# Sweep 5: QEMU 11.0.1 *linux-user* x86-64 -> aarch64, run inside the aarch64
# HVF guest. qemu-user has no software MMU, so (qemu-user / aarch64-in-guest)
# is TCG code-generation quality with the softmmu cost removed -- which is what
# doc 12's "QEMU 16% of native on 7-zip" figure actually measures.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RES="$HERE/results"; mkdir -p "$RES"
for r in 1 2 3; do
  "$HERE/runvm.py" --arch aarch64 --accel hvf --task qemuuser --quiet --timeout 3000 \
    --out "$RES/qemuuser.$r.json" >/dev/null 2>&1
done
echo "SWEEP5 DONE"
