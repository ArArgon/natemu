#!/bin/bash
# qrun.sh OUTDIR NAME EMU VCPUS BENCH: one bench.py run under the VM lock; waits (retries) while another emulator is running.
here=$(dirname "$(realpath "$0")"); RAW=${NATEMU_RAW_ROOT:-$(cd "$here/../../.." && python3 -c 'from raw_root import RAW_ROOT; print(RAW_ROOT)')}
for try in $(seq 1 180); do
    rc=0; /usr/bin/lockf -k $RAW/.vm.lock "$here/qcheck.sh" python3 "$here/bench.py" "$@" || rc=$?
    [ $rc = 75 ] || exit $rc; sleep 20
done
exit 75
