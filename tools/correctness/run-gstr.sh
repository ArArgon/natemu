#!/bin/sh
# run-gstr.sh DIR [K/N]: run-libm.sh for glibc's string and wcsmbs tests: the exact commands of DIR/gstr-cmds.txt (from
# the native `make check subdirs=string` / `subdirs=wcsmbs`; entries K, K+N, ...) against the build tree's ld.so and libc,
# with glibc's own timeout scaled by TIMEOUTFACTOR and a kill timeout. glibc's verdicts: exit 0 PASS, 77 UNSUPPORTED.
#   CT-RESULT gstr NAME rc=EXIT     (rc=skip: listed in DIR/gstr-skip.txt, if present)
#   CT-OUT NAME | ...   the first 60 output lines of a test whose rc is neither 0 nor 77
D=$1; k=${2%/*}; n=${2#*/}; [ -n "$2" ] || { k=0; n=1; }
export TIMEOUTFACTOR=${TIMEOUTFACTOR:-10}; i=0; o=${TMPDIR:-/tmp}/gstr-out.$$
tab=$(printf '\t')
while IFS="$tab" read -r name cwd cmd; do
    [ $((i % n)) = "$k" ] || { i=$((i + 1)); continue; }; i=$((i + 1))
    if [ -f "$D/gstr-skip.txt" ] && grep -qx "$name" "$D/gstr-skip.txt"; then echo "CT-RESULT gstr $name rc=skip"; continue; fi
    mkdir -p "$cwd"
    (cd "$cwd" && exec timeout -s KILL 1200 sh -c "exec $cmd") > "$o" 2>&1 < /dev/null; rc=$?
    echo "CT-RESULT gstr $name rc=$rc"
    case $rc in 0|77) ;; *) head -n 60 "$o" | sed "s|^|CT-OUT $name \| |";; esac
done < "$D/gstr-cmds.txt"
rm -f "$o"
