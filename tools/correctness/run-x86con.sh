#!/bin/sh
# run-x86con.sh DIR K/N: run the one kernel x86 selftest DIR/x86st/* number K (of N, sorted) with its output straight
# to the console (line-buffered), to see where a test that stops the machine was (no CT-RESULT if it stops).
D=$1; k=${2%/*}; i=0
for f in $(ls "$D/x86st" | sort); do
    [ $i = "$k" ] && { echo "x86con: $f"; (cd /tmp && "$D/x86st/$f" < /dev/null); echo "CT-RESULT x86con $f rc=$?"; }
    i=$((i + 1))
done
