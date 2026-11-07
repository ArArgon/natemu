#!/bin/sh
# run-cpyb.sh DIR [PART]: in the guest, build CPython from the pristine source at W/py (W = /home/ubuntu/cp, as on the
# AMD machine) with cpy-build.sh at make -j NPROC: CT-RESULT cpyb lines per step and CT-HASH lines per build output
# (compared with the native build's), then the Makefile and _sysconfigdata (CT-FILE). A failed step's log is printed
# (CT-LOG); with PART `smoke` (the default) the
# built interpreter then runs the tests of DIR/cpy-tests.txt marked `smoke` (run-cpy.sh).
W=/home/ubuntu/cp; L=/tmp/pylog
bash "$1/cpy-build.sh" "$W" "$(nproc)" "$L"; rc=$?
for f in "$L"/*.log; do echo "CT-LOG $f lines=$(wc -l < "$f")"; done
[ $rc = 0 ] || { tail -n 80 "$L"/*.log | sed 's/^/CT-LOG | /'; exit $rc; }
grep -E ' warning: |necessary bits|Could not build|failed to build|failed on import' "$L/make.log" | head -n 40 | sed 's/^/CT-LOG make | /'
# The build outputs that record their environment (Makefile, _sysconfigdata), whole, to explain hash differences.
for f in "$W/py/Makefile" "$W"/py/build/lib.*/_sysconfigdata_*.py; do sed "s|^|CT-FILE ${f#$W/py/} \| |" "$f"; done
[ "${2:-smoke}" = none ] || PY=$W/py sh "$1/run-cpy.sh" "$1" smoke
