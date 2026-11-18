#!/bin/bash
# campaign.sh OUTDIR [ROUNDS]: the benchmarks on QEMU, like-for-like (qemu-multi; pass qemu-multi-max by editing the loop for -cpu max), N = 1 and 4,
# ROUNDS (default 3) interleaved rounds; each run verified by bench.py (exit 0 = verified).
here=$(dirname "$(realpath "$0")"); out=$1
for r in $(seq ${2:-3}); do for b in build zstd hackbench locks luajit luajit-interp mem cray 7zip sqlite avx; do for n in 1 4; do for m in multi; do
    "$here/qrun.sh" "$out" "$b-$n-$m-r$r" qemu-$m $n $b || echo "FAILED $b $n $m r$r rc=$?"
done; done; done; done
