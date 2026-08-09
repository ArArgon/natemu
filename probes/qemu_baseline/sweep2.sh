#!/bin/bash
# Second sweep: MTTCG, QEMU knob sensitivity, and 7-Zip 26.01 in all four
# environments. Run after sweep.sh; requires ./build.sh to have been re-run
# with the par*/sevenzip tasks and the 7-Zip overlay in place.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RES="$HERE/results"; mkdir -p "$RES"
REPS="${REPS:-3}"
log() { echo "### $* :: $(date +%H:%M:%S)"; }

# ---------- A. MTTCG: single vs multi at smp 1/2/4 --------------------------
# The guest task runs N independent single-threaded workers, so a perfect
# MTTCG would keep wall time flat as N (and -smp) rise.
for th in single multi; do
  for n in 1 2 4; do
    for r in $(seq 1 $REPS); do
      log "mttcg $th smp$n par$n rep$r"
      "$HERE/runvm.py" --arch x86_64 --accel tcg --tcg-thread "$th" --smp "$n" \
        --task "par$n" --quiet --timeout 3000 \
        --out "$RES/mttcg.$th.smp$n.$r.json" >/dev/null 2>&1
    done
  done
done

# ---------- B. QEMU knob sensitivity ---------------------------------------
for r in $(seq 1 $REPS); do
  log "cpu=qemu64 rep$r"
  "$HERE/runvm.py" --arch x86_64 --accel tcg --cpu qemu64 --task bench \
    --quiet --timeout 3000 --out "$RES/knob.cpu-qemu64.$r.json" >/dev/null 2>&1
  log "tb-size=1024 rep$r"
  "$HERE/runvm.py" --arch x86_64 --accel tcg --tb-size 1024 --task bench \
    --quiet --timeout 3000 --out "$RES/knob.tb1024.$r.json" >/dev/null 2>&1
done
for r in $(seq 1 $REPS); do
  log "thp=never rep$r"
  "$HERE/runvm.py" --arch x86_64 --accel tcg --task bench \
    --extra-append "transparent_hugepage=never" \
    --quiet --timeout 3000 --out "$RES/knob.thpoff.$r.json" >/dev/null 2>&1
  "$HERE/runvm.py" --arch aarch64 --accel hvf --task bench \
    --extra-append "transparent_hugepage=never" \
    --quiet --timeout 3000 --out "$RES/knob.thpoff-hvf.$r.json" >/dev/null 2>&1
done

# ---------- C. 7-Zip 26.01, same upstream release everywhere ----------------
SZ="$HERE/work/mac7z/7zz"
for r in $(seq 1 $REPS); do
  log "7z native arm64 rep$r"
  arch -arm64 "$SZ" b -mmt1 -md=22 > "$RES/7z.native-arm64.$r.txt" 2>&1
  log "7z rosetta x86_64 rep$r"
  arch -x86_64 "$SZ" b -mmt1 -md=22 > "$RES/7z.rosetta-x64.$r.txt" 2>&1
done
for r in $(seq 1 $REPS); do
  log "7z aarch64-hvf rep$r"
  "$HERE/runvm.py" --arch aarch64 --accel hvf --task sevenzip --quiet \
    --timeout 3000 --out "$RES/7z.aarch64-hvf.$r.json" >/dev/null 2>&1
done
for r in $(seq 1 $REPS); do
  log "7z x86_64-tcg rep$r"
  "$HERE/runvm.py" --arch x86_64 --accel tcg --task sevenzip --quiet \
    --timeout 6000 --out "$RES/7z.x86_64-tcg.$r.json" >/dev/null 2>&1
done
log "SWEEP2 DONE"
