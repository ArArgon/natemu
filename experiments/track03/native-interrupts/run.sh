#!/bin/sh
# usage: run.sh OUTDIR [load]   builds OUTDIR/probe and runs the matrix reported in README.md into OUTDIR/final[-load].txt
#   load: 12 busy `yes` processes during the run (oversubscribes the 10-core M4 like stop-premise's spinners)
set -e; here=$(cd "$(dirname "$0")" && pwd); out=$1; sh "$here/build.sh" "$out" >/dev/null 2>&1
f=$out/final.txt; pids=
if [ "$2" = load ]; then f=$out/final-load.txt; for i in 1 2 3 4 5 6 7 8 9 10 11 12; do yes >/dev/null & pids="$pids $!"; done; fi
{ sw_vers; sysctl -n machdep.cpu.brand_string
  for m in "checks 1" "checks 1 gic" "inj 1 level" "inj 4000" "spi 4000" "vt 4000" "vt 4000 gic" "sgi 4000" "sgi 2000 wfi" \
           "wfi 1000 gic" "wfi 500 park=1000" "stop 4000" "stop 4000 is" "stop 4000 asid" "stop 4000 patch" "armbench 1000000" "armbench 1000000 is" \
           "armbench 1000000 asid" "armbench 1000000 patch" \
           "tput 500" "tput 500 nopad" "tput 500 ldchk" "stop 4000 unlink" "stop 4000 xn" "stop 4000 ldchk" "stop 4000 ldchk asid" "stop 4000 xn asid"; do echo "=== probe $m"; "$out/probe" $m; done; } > "$f" 2>&1 || true
[ -n "$pids" ] && kill $pids
echo "wrote $f"
