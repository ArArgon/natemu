#!/bin/bash
# diverge_demo.sh -- the whole divergence-hunting loop, end to end, on a real
# Linux boot: two differently-configured x86-64 emulators, a checkpointed state
# hash from each, a bisect to the 65 536-instruction window that first differs,
# a windowed full-register re-run, and the exact first differing instruction.
#
#   ./diverge_demo.sh                     # qemu64 vs Nehalem (a real difference)
#   CPUB=qemu64 ./diverge_demo.sh         # identical configs -> proves determinism
#
# Determinism recipe (measured, see doc 25 sec 2):
#   -icount shift=3,sleep=off  -rtc base=<fixed>,clock=vm  -smp 1
# Without both of those, two identical QEMU boots diverge after ~1.7 M of
# 1.18 G instructions and the oracle is worthless.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
QW="$HERE/../qemu_baseline/work"
P="$HERE/plugin_state.so"
CPUA="${CPUA:-qemu64}"
CPUB="${CPUB:-Nehalem}"
DET="-icount shift=3,sleep=off -rtc base=2020-01-01,clock=vm"

boot() { # $1=cpu  $2=ckptfile  $3.. = extra plugin args
    local cpu="$1" out="$2"; shift 2
    local pargs="mode=pc,ckpt=16,out=$out"
    for a in "$@"; do pargs="$pargs,$a"; done
    qemu-system-x86_64 -accel tcg $DET -machine q35 -cpu "$cpu" -m 1024 -smp 1 \
        -kernel "$QW/nb-x86_64/boot/vmlinuz-virt" -initrd "$QW/initramfs-x86_64.cpio" \
        -append "console=ttyS0 natemu.task=none panic=-1 rdinit=/init loglevel=3" \
        -display none -nodefaults -monitor none -no-reboot -serial stdio \
        -plugin "$P,$pargs" -d plugin >/dev/null 2>>/tmp/dd.log
}

echo "== stage 1: checkpointed state hash, A=$CPUA B=$CPUB"
: > /tmp/dd.log
time boot "$CPUA" /tmp/dd_a.ckpt
time boot "$CPUB" /tmp/dd_b.ckpt
grep STATEHASH /tmp/dd.log
ls -l /tmp/dd_a.ckpt /tmp/dd_b.ckpt | awk '{print "  ckpt bytes:", $5, $9}'

echo
echo "== stage 2: bisect to the window"
set +e
OUT=$(python3 "$HERE/bisect.py" ckpt /tmp/dd_a.ckpt /tmp/dd_b.ckpt); RC=$?
set -e
echo "$OUT"
[ "$RC" = 0 ] && exit 0

LO=$(echo "$OUT" | sed -n 's/.*wlo=\([0-9]*\),whi=.*/\1/p')
HI=$(echo "$OUT" | sed -n 's/.*whi=\([0-9]*\),wout=.*/\1/p')
[ -n "$LO" ] || exit 1

echo
echo "== stage 3: re-run both with a full-register window dump [$LO,$HI)"
boot "$CPUA" /tmp/dd_a2.ckpt "wlo=$LO" "whi=$HI" "wout=/tmp/dd_a.win"
boot "$CPUB" /tmp/dd_b2.ckpt "wlo=$LO" "whi=$HI" "wout=/tmp/dd_b.win"
ls -l /tmp/dd_a.win | awk '{print "  window bytes:", $5}'

echo
echo "== stage 4: first differing instruction"
python3 "$HERE/bisect.py" win /tmp/dd_a.win /tmp/dd_b.win
