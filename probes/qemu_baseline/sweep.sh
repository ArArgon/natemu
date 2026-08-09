#!/bin/bash
# Full measurement sweep for docs/research/15-qemu-tcg-baseline.md.
# Everything is run sequentially; nothing else should be running on the host.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RES="$HERE/results"
mkdir -p "$RES"
REPS="${REPS:-3}"

therm() { pmset -g therm 2>/dev/null | tr '\n' ';' ; }

log() { echo "### $* :: $(date +%H:%M:%S) :: $(therm)"; }

# ---------- 0. environment capture -----------------------------------------
{
  echo "date: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "sw_vers: $(sw_vers -productVersion) $(sw_vers -buildVersion)"
  echo "uname: $(uname -a)"
  echo "cpu: $(sysctl -n machdep.cpu.brand_string)"
  echo "ncpu: $(sysctl -n hw.ncpu) perflevel0(P): $(sysctl -n hw.perflevel0.logicalcpu) perflevel1(E): $(sysctl -n hw.perflevel1.logicalcpu)"
  echo "memsize: $(sysctl -n hw.memsize)"
  echo "pagesize: $(sysctl -n hw.pagesize)"
  echo "qemu: $(qemu-system-x86_64 --version | head -1)"
  echo "qemu-brew: $(brew list --versions qemu)"
  echo "zig: $(zig version)"
  echo "therm-start: $(therm)"
  echo "loadavg: $(sysctl -n vm.loadavg)"
} > "$RES/env.txt"
cat "$RES/env.txt"

# ---------- 1. host-native (macOS arm64) and Rosetta 2 ----------------------
for r in $(seq 1 $REPS); do
  log "native arm64 rep$r"
  "$HERE/bin/bench.macos-arm64" all > "$RES/native-arm64.$r.txt" 2>&1
done
for r in $(seq 1 $REPS); do
  log "rosetta x86_64 rep$r"
  arch -x86_64 "$HERE/bin/bench.macos-x64" all > "$RES/rosetta-x64.$r.txt" 2>&1
done
# E-core control: -b requests background QoS, which macOS schedules on E cores
log "native arm64 E-core(bg QoS) control"
taskpolicy -b "$HERE/bin/bench.macos-arm64" alu alu4 ls4 > "$RES/native-arm64-ecore.txt" 2>&1

# ---------- 2. boot-only timings -------------------------------------------
for r in $(seq 1 5); do
  "$HERE/runvm.py" --arch aarch64 --accel hvf --task none --quiet \
      --out "$RES/boot.aarch64-hvf.$r.json" >/dev/null 2>&1
  "$HERE/runvm.py" --arch x86_64 --accel tcg --task none --quiet \
      --out "$RES/boot.x86_64-tcg.$r.json" >/dev/null 2>&1
  "$HERE/runvm.py" --arch aarch64 --accel tcg --task none --quiet \
      --out "$RES/boot.aarch64-tcg.$r.json" >/dev/null 2>&1
done
log "boot-only done"

# ---------- 3. the four guest configurations -------------------------------
run_guest() { # arch accel thread label extra...
  local arch="$1" accel="$2" thread="$3" label="$4"; shift 4
  for r in $(seq 1 $REPS); do
    log "$label rep$r"
    "$HERE/runvm.py" --arch "$arch" --accel "$accel" --tcg-thread "$thread" \
        --task bench --quiet --timeout 3000 "$@" \
        --out "$RES/$label.$r.json" >/dev/null 2>&1
  done
}

run_guest aarch64 hvf single aarch64-hvf
run_guest x86_64  tcg single x86_64-tcg-single
run_guest aarch64 tcg single aarch64-tcg-single

log "SWEEP CORE DONE"
