#!/bin/sh
# run-i386.sh DIR PART: run the 32-bit programs DIR/i386/tests/* (PART smoke: hello.static alone; any other word: the
# others), sorted, each in an empty
# directory with a 900 s kill timeout, the argument `one` and I386_ENV=set. Per test: its output as
# `CT-LOG NAME | LINE` lines, then
#   CT-RESULT i386 NAME rc=EXIT out=MD5-PREFIX sec=WALL-SECONDS
# The same script runs in the image's root on the x86 reference machine to record the expected outputs
# (testkit/harness/campaign.py, native).
D=$1/i386; T=${TMPDIR:-/tmp}/ct-i386
for f in $(ls "$D/tests" | sort); do
    case $2:$f in smoke:hello.static) ;; smoke:* | *:hello.static) continue;; esac
    rm -rf "$T"; mkdir -p "$T"
    t0=$(date +%s.%N)
    (cd "$T" && I386_ENV=set exec timeout -s KILL 900 "$D/tests/$f" one) > "$T.out" 2>&1 < /dev/null; rc=$?
    sed "s/^/CT-LOG $f | /" "$T.out"
    echo "CT-RESULT i386 $f rc=$rc out=$(md5sum < "$T.out" | cut -c1-12) sec=$(echo "$t0 $(date +%s.%N)" | awk '{printf "%.2f", $2 - $1}')"
done
rm -rf "$T" "$T.out"
