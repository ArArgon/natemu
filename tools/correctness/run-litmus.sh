#!/bin/sh
# run-litmus.sh DIR [K/N[,a=A][,r=R][,s=S]]: run the litmus7 harnesses DIR/lit/FAMILY.NAME (every N-th from K) with
# litmus options -a A (default: the CPU count) -r R -s S (defaults: the compiled -r 100 -s 100000); tests with more
# threads than A (DIR/lit/threads.txt) are skipped (oversubscribed spin barriers take hours). Per test, its output as
# `CT-LOG FAMILY.NAME | LINE` lines (the histogram is what litmus-check.py reads), then
#   CT-RESULT litmus FAMILY.NAME rc=EXIT obs=Never|Sometimes|Always sec=S
# and `CT-SKIP litmus FAMILY.NAME threads=T` for a skipped test. Same script natively. l=FILE (relative to DIR): run only
# the tests named in FILE (one FAMILY.NAME per line), e.g. a rerun with a larger -r of the conditions never observed.
D=$1; p=${2:-0/1}; kn=${p%%,*}; k=${kn%/*}; n=${kn#*/}; a=$(nproc); r=; s=; l=
for o in $(echo "$p" | tr , ' '); do case $o in a=*) a=${o#a=};; l=*) l=$D/${o#l=};; r=*) r="-r ${o#r=}";; s=*) s="-s ${o#s=}";; esac; done
T=${TMPDIR:-/tmp}/ct-litmus; mkdir -p "$T"; i=0
echo "CT-LITMUS a=$a $r $s part=$kn list=${l:-all}"
for f in $(ls "$D/lit" | grep -v '^threads.txt$' | sort); do
    [ $((i % n)) = "$k" ] || { i=$((i + 1)); continue; }; i=$((i + 1))
    [ -z "$l" ] || grep -qxF "$f" "$l" || continue
    th=$(awk -v t="$f" '{ sub("/", ".", $1) } $1 == t { print $2 }' "$D/lit/threads.txt")
    [ "${th:-1}" -le "$a" ] || { echo "CT-SKIP litmus $f threads=$th"; continue; }
    t0=$(date +%s)
    (cd "$T" && exec timeout -s KILL 900 "$D/lit/$f" -a "$a" $r $s) > "$T/.out" 2>&1 < /dev/null; rc=$?
    sed "s/^/CT-LOG $f | /" "$T/.out"
    echo "CT-RESULT litmus $f rc=$rc obs=$(sed -n 's/^Observation [^ ]* \([A-Za-z]*\) .*/\1/p' "$T/.out") sec=$(( $(date +%s) - t0 ))"
done
rm -rf "$T"
