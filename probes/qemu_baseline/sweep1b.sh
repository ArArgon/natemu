#!/bin/bash
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RES="$HERE/results"; mkdir -p "$RES"
log(){ echo "### $* $(date +%H:%M:%S) $(pmset -g therm 2>/dev/null | tr '\n' ';')"; }
for r in 1 2 3; do
  log "native rep$r";  "$HERE/bin/bench.macos-arm64" all > "$RES/native-arm64.$r.txt" 2>&1
  log "rosetta rep$r"; arch -x86_64 "$HERE/bin/bench.macos-x64" all > "$RES/rosetta-x64.$r.txt" 2>&1
done
for r in 1 2 3 4 5; do
  for c in "aarch64 hvf" "x86_64 tcg" "aarch64 tcg"; do
    set -- $c
    "$HERE/runvm.py" --arch $1 --accel $2 --task none --quiet --out "$RES/boot.$1-$2.$r.json" >/dev/null 2>&1
  done
done
log "boot done"
for r in 1 2 3; do
  log "hvf rep$r";     "$HERE/runvm.py" --arch aarch64 --accel hvf --task bench --quiet --timeout 3000 --out "$RES/aarch64-hvf.$r.json" >/dev/null 2>&1
  log "x86tcg rep$r";  "$HERE/runvm.py" --arch x86_64  --accel tcg --task bench --quiet --timeout 3000 --out "$RES/x86_64-tcg-single.$r.json" >/dev/null 2>&1
  log "armtcg rep$r";  "$HERE/runvm.py" --arch aarch64 --accel tcg --task bench --quiet --timeout 3000 --out "$RES/aarch64-tcg-single.$r.json" >/dev/null 2>&1
done
log "SWEEP1B DONE"
