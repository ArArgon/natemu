#!/bin/bash
# Sweep 4: fill in the profile/plugin coverage gaps (alu, ptr8, matmul, the full
# suite, and 7-Zip) so the softmmu-share table has no missing rows.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="$HERE/results/profile"; mkdir -p "$OUT"
QEMU=/opt/homebrew/bin/qemu-system-x86_64
KERN="$HERE/work/nb-x86_64/boot/vmlinuz-virt"
IRD="$HERE/work/initramfs-x86_64.cpio"

# --- extra host profiles
for entry in "matmul:matmul,matmul,matmul,matmul,matmul,matmul" \
             "ptr8:ptr8,ptr8,ptr8,ptr8,ptr8,ptr8,ptr8,ptr8" \
             "alu:alu,alu,alu,alu,alu,alu,alu,alu,alu,alu,alu,alu,alu,alu,alu,alu,alu,alu,alu,alu,alu,alu,alu,alu,alu" \
             "sevenzip:SEVENZIP"; do
  w="${entry%%:*}"; list="${entry#*:}"
  if [ "$list" = "SEVENZIP" ]; then TASK="sevenzip"; else TASK="bench:$list"; fi
  echo "== sampling $w"
  ( exec "$QEMU" -machine q35 -accel tcg,thread=single -cpu max -m 3072 -smp 1 \
      -kernel "$KERN" -initrd "$IRD" \
      -append "console=ttyS0 natemu.task=$TASK panic=-1 rdinit=/init loglevel=3" \
      -display none -serial stdio -no-reboot -nodefaults -monitor none ) > "$OUT/run.$w.log" 2>&1 &
  PID=$!; sleep 8
  kill -0 $PID 2>/dev/null && { sample $PID 12 1 -f "$OUT/sample.$w.txt" >/dev/null 2>&1 || echo "  sample FAILED $w"; }
  kill $PID 2>/dev/null; wait $PID 2>/dev/null
done

# --- extra plugin counts
for w in alu ptr8; do
  echo "== plugin count $w"
  "$QEMU" -machine q35 -accel tcg,thread=single -cpu max -m 3072 -smp 1 \
      -plugin "$HERE/bin/plugin_count.so" -d plugin -D "$OUT/plugin.$w.txt" \
      -kernel "$KERN" -initrd "$IRD" \
      -append "console=ttyS0 natemu.task=bench:$w panic=-1 rdinit=/init loglevel=3" \
      -display none -serial stdio -no-reboot -nodefaults -monitor none > "$OUT/pluginrun.$w.log" 2>&1
done
echo "== plugin count sevenzip"
"$QEMU" -machine q35 -accel tcg,thread=single -cpu max -m 3072 -smp 1 \
    -plugin "$HERE/bin/plugin_count.so" -d plugin -D "$OUT/plugin.sevenzip.txt" \
    -kernel "$KERN" -initrd "$IRD" \
    -append "console=ttyS0 natemu.task=sevenzip panic=-1 rdinit=/init loglevel=3" \
    -display none -serial stdio -no-reboot -nodefaults -monitor none > "$OUT/pluginrun.sevenzip.log" 2>&1
echo "SWEEP4 DONE"
