#!/bin/sh
# run-x86st.sh DIR [K/N]: run the kernel x86 selftests DIR/x86st/*_64 (every N-th from K), each in an empty directory
# with a 300 s kill timeout (ldt_gdt_32: 60 s). Per test: its output as `CT-LOG NAME | LINE` lines, then
#   CT-RESULT x86st NAME rc=EXIT ok=#[OK] fail=#[FAIL] skip=#[SKIP] out=MD5-PREFIX
# ([OK]/[FAIL]/[SKIP] count the tests' own result lines, kselftest "ok"/"not ok" lines included). Same script natively.
# A test named in DIR/x86st-skip.txt is not run (`CT-SKIP x86st NAME`): one that wedges the whole machine.
D=$1; k=${2%/*}; n=${2#*/}; [ -n "$2" ] || { k=0; n=1; }
T=${TMPDIR:-/tmp}/ct-x86st; i=0
for f in $(ls "$D/x86st" | sort); do
    [ $((i % n)) = "$k" ] || { i=$((i + 1)); continue; }; i=$((i + 1))
    grep -qx "$f" "$D/x86st-skip.txt" 2>/dev/null && { echo "CT-SKIP x86st $f"; continue; }
    rm -rf "$T"; mkdir -p "$T"
    # ldt_gdt_32: its last test loads a non-flat DS, which natemu does not run before M2 task 6; a stale handler then
    # loops for ever (docs/restructure/m2-checkpoint-a-findings.md). It ends in under 5 s natively: 60 s is ample.
    t=300; [ "$f" != ldt_gdt_32 ] || t=60
    (cd "$T" && exec timeout -s KILL $t "$D/x86st/$f") > "$T.out" 2>&1 < /dev/null; rc=$?
    sed "s/^/CT-LOG $f | /" "$T.out"
    c() { grep -c -E "$1" "$T.out"; }
    echo "CT-RESULT x86st $f rc=$rc ok=$(c '\[OK\]|^ok ') fail=$(c '\[FAIL\]|^not ok ') skip=$(c '\[SKIP\]|# SKIP') out=$(md5sum < "$T.out" | cut -c1-12)"
done
rm -rf "$T" "$T.out"
