#!/bin/bash
# Sweep 6: warm-up / QEMU-software-TLB-sizing check.
# ptr8 costs 3.38 s the FIRST time it runs in a VM and 0.39 s on every later
# repetition in the same process -- an 8.7x swing with no change in the work
# done. Establish whether the same is true of the two workloads the headline
# softmmu conclusions rest on.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RES="$HERE/results"; mkdir -p "$RES"
for spec in "ptr8:ptr8,ptr8,ptr8,ptr8,ptr8,ptr8" \
            "ls4tlb:ls4tlb,ls4tlb,ls4tlb,ls4tlb" \
            "ptr256:ptr256,ptr256,ptr256" \
            "stream:stream,stream,stream" \
            "ls4:ls4,ls4,ls4"; do
  w="${spec%%:*}"; list="${spec#*:}"
  "$HERE/runvm.py" --arch x86_64 --accel tcg --task "bench:$list" --quiet --timeout 3000 \
    --out "$RES/warm.$w.json" >/dev/null 2>&1
  "$HERE/runvm.py" --arch aarch64 --accel hvf --task "bench:$list" --quiet --timeout 3000 \
    --out "$RES/warm-hvf.$w.json" >/dev/null 2>&1
done
echo "SWEEP6 DONE"
