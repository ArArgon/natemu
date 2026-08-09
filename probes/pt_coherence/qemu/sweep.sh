#!/bin/bash
# pt_coherence sweep: x86-64 Linux guest under QEMU TCG.
# Group P = with plugin_inv.so (instruction-class counts, ~4x slower).
# Group N = no plugin        (wall-clock-accurate rates + guest /proc deltas).
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
R="$HERE/results"
INITRD="$HERE/combined-x86_64.cpio"
mkdir -p "$R"

runp() { # label task smp thread
    local lbl="$1" task="$2" smp="$3" thr="${4:-single}"
    echo "=== P $lbl (task=$task smp=$smp $thr)"
    python3 "$HERE/runvm.py" --arch x86_64 --accel tcg --task "$task" \
        --smp "$smp" --tcg-thread "$thr" --mem 2048 --timeout 1200 --quiet \
        --initrd "$INITRD" --label "$lbl" --out "$R/P.$lbl.json" \
        --qemu-debug plugin --qemu-debug-file "$R/P.$lbl.plugin.txt" \
        -- -plugin "$HERE/plugin_inv.so" > "$R/P.$lbl.run.json" 2>&1
}

runn() { # label task smp thread
    local lbl="$1" task="$2" smp="$3" thr="${4:-single}"
    echo "=== N $lbl (task=$task smp=$smp $thr)"
    python3 "$HERE/runvm.py" --arch x86_64 --accel tcg --task "$task" \
        --smp "$smp" --tcg-thread "$thr" --mem 2048 --timeout 1200 --quiet \
        --initrd "$INITRD" --label "$lbl" --out "$R/N.$lbl.json" \
        > "$R/N.$lbl.run.json" 2>&1
}

MODE="${1:-all}"

if [ "$MODE" = all ] || [ "$MODE" = p ]; then
  runp boot.smp1        none        1 single
  runp spawn.smp1       spawn2000   1 single
  runp sevenzip.smp1    sevenzip    1 single
  runp bench.smp1       bench       1 single
  runp boot.smp4        none        4 multi
  runp spawn.smp4       spawn2000   4 multi
  runp sevenzipmt.smp4  sevenzipmt4 4 multi
fi

if [ "$MODE" = all ] || [ "$MODE" = n ]; then
  runn boot.smp1        none        1 single
  runn spawn.smp1       spawn2000   1 single
  runn sevenzip.smp1    sevenzip    1 single
  runn bench.smp1       bench       1 single
  runn boot.smp4        none        4 multi
  runn spawn.smp4       spawn2000   4 multi
  runn sevenzipmt.smp4  sevenzipmt4 4 multi
fi
echo DONE
