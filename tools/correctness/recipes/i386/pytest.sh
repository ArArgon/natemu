#!/bin/sh
# pytest.sh MODULE [REGRTEST OPTIONS]: one module of CPython's regression tests under the i386 interpreter (options:
# -i PATTERN leaves cases out, -m PATTERN runs only those). Prints regrtest's verdict,
# and for a module that failed its output without the lines that vary between runs (times, the machine).
m=$1; shift
/usr/bin/python3.13-i386 -m test -W "$@" "$m" > out.txt 2>&1; rc=$?
[ $rc = 0 ] || grep -v -E '^(== |Using random seed|[0-9]+:[0-9]+:[0-9]+ |Total duration|Ran [0-9]+ tests? in |$)' out.txt | cut -c1-300 | head -150
echo "$m: $(sed -n 's/^Result: //p' out.txt | tail -1) rc=$rc"
