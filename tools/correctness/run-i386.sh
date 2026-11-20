#!/bin/sh
# run-i386.sh DIR [K/N]: run the 32-bit programs DIR/i386/tests/* (every N-th from K, sorted), each in an empty
# directory with a 900 s kill timeout, the argument `one` and I386_ENV=set. Per test: its output as
# `CT-LOG NAME | LINE` lines, then
#   CT-RESULT i386 NAME rc=EXIT out=MD5-PREFIX
# The same script runs in the image's root on the x86 reference machine to record the expected outputs
# (testkit/harness/campaign.py, native).
D=$1/i386; k=${2%/*}; n=${2#*/}; [ -n "$2" ] || { k=0; n=1; }
T=${TMPDIR:-/tmp}/ct-i386; i=0
for f in $(ls "$D/tests" | sort); do
    [ $((i % n)) = "$k" ] || { i=$((i + 1)); continue; }; i=$((i + 1))
    rm -rf "$T"; mkdir -p "$T"
    (cd "$T" && I386_ENV=set exec timeout -s KILL 900 "$D/tests/$f" one) > "$T.out" 2>&1 < /dev/null; rc=$?
    sed "s/^/CT-LOG $f | /" "$T.out"
    echo "CT-RESULT i386 $f rc=$rc out=$(md5sum < "$T.out" | cut -c1-12)"
done
rm -rf "$T" "$T.out"
