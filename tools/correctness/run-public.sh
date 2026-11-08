#!/bin/sh
# One manifest entry per boot; all stdout/stderr remains on the serial console.
set -u
D=$1; index=${2%/*}
line=$(sed -n "$((index + 1))p" "$D/public-cases.tsv")
test_id=$(printf '%s\n' "$line" | cut -f1)
command=$(printf '%s\n' "$line" | cut -f2-)
[ -n "$test_id" ] && [ -n "$command" ] || exit 2
cap=300
for a in $(cat /proc/cmdline); do case "$a" in pubcap=*) cap=${a#pubcap=};; esac; done
export LD_LIBRARY_PATH=/opt/public/rseq
export PATH=/opt/ltp/testcases/bin:$PATH LTPROOT=/opt/ltp LTP_COLORIZE_OUTPUT=0
export LTP_TIMEOUT_MUL=4 TMPDIR=/tmp
echo "PUB-START id=$test_id cap=$cap"
timeout -k 5 "$cap" sh -c "$command"
rc=$?
echo "PUB-RESULT id=$test_id rc=$rc"
exit "$rc"
