#!/bin/bash
# isaclass_run.sh -- run the read-only plugin_isaclass.so over the existing
# x86-64 Alpine guest for doc 24.  Adds nothing to the harness; reuses the
# kernel/initramfs that build.sh already produced in ./work.
#   ./isaclass_run.sh <label> <natemu.task>
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK="$HERE/work"; OUT="$HERE/results/isaclass"; mkdir -p "$OUT"
QEMU=${QEMU:-/opt/homebrew/bin/qemu-system-x86_64}
L="$1"; T="${2:-none}"; CPU="${3:-max}"
"$QEMU" -machine q35 -accel tcg,thread=single -cpu "$CPU" -m 3072 -smp 1 \
    -plugin "$HERE/bin/plugin_isaclass.so" -d plugin -D "$OUT/isaclass.$L.txt" \
    -kernel "$WORK/nb-x86_64/boot/vmlinuz-virt" \
    -initrd "$WORK/initramfs-x86_64.cpio" \
    -append "console=ttyS0 natemu.task=$T panic=-1 rdinit=/init loglevel=7" \
    -display none -serial stdio -no-reboot -nodefaults -monitor none \
    > "$OUT/run.$L.log" 2>&1
echo "exit=$? label=$L task=$T"
