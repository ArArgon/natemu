#!/bin/sh
# run-libm.sh DIR [K/N]: run the glibc math tests of DIR/libm-cmds.txt (NAME<TAB>CWD<TAB>COMMAND: the exact commands the
# native `make check subdirs=math` ran, against the build tree's ld.so and libm; entries K, K+N, ...), each with glibc's
# own timeout scaled by TIMEOUTFACTOR and a kill timeout. glibc's verdicts: exit 0 PASS, 77 UNSUPPORTED, other FAIL.
#   CT-RESULT libm NAME rc=EXIT     (rc=skip: listed in DIR/libm-skip.txt, if present)
#   CT-OUT NAME | ...   the first 60 output lines of a test whose rc is neither 0 nor 77
D=$1; k=${2%/*}; n=${2#*/}; [ -n "$2" ] || { k=0; n=1; }
export TIMEOUTFACTOR=${TIMEOUTFACTOR:-10}; i=0; o=${TMPDIR:-/tmp}/libm-out.$$
tab=$(printf '\t')
while IFS="$tab" read -r name cwd cmd; do
    [ $((i % n)) = "$k" ] || { i=$((i + 1)); continue; }; i=$((i + 1))
    if [ -f "$D/libm-skip.txt" ] && grep -qx "$name" "$D/libm-skip.txt"; then echo "CT-RESULT libm $name rc=skip"; continue; fi
    mkdir -p "$cwd"
    (cd "$cwd" && exec timeout -s KILL 1200 sh -c "exec $cmd") > "$o" 2>&1 < /dev/null; rc=$?
    echo "CT-RESULT libm $name rc=$rc"
    case $rc in 0|77) ;; *) head -n 60 "$o" | sed "s|^|CT-OUT $name \| |";; esac
done < "$D/libm-cmds.txt"
rm -f "$o"
