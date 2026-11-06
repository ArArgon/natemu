#!/bin/sh
# run-ltp.sh DIR [K/N]: run the LTP entries of DIR/ltp-list.txt (RUNTEST<TAB>NAME<TAB>COMMAND; entries K, K+N, ...) the
# way runltp does (sh -c COMMAND, PATH and LTPROOT set), each in its own temporary directory with a kill timeout.
#   CT-RESULT ltp RUNTEST:NAME rc=EXIT sec=WALL
#   CT-OUT RUNTEST:NAME | ...   the last 40 output lines of an entry whose rc is neither 0 (pass) nor 32 (TCONF)
# LTP exit codes: 0 pass, 1 TFAIL, 2 TBROK, 4 TWARN, 32 TCONF (ored); 137 = killed at the outer timeout.
# LTPROOT defaults to /opt/ltp (the guest); the native run sets it to the build's install.
D=$1; k=${2%/*}; n=${2#*/}; [ -n "$2" ] || { k=0; n=1; }
export LTPROOT=${LTPROOT:-/opt/ltp} LTP_TIMEOUT_MUL=${LTP_TIMEOUT_MUL:-4} LTP_RUNTIME_MUL=${LTP_RUNTIME_MUL:-0.2}
export PATH=$LTPROOT/testcases/bin:$PATH LTP_COLORIZE_OUTPUT=0
OUTER=${CT_OUTER:-900}; base=${TMPDIR:-/tmp}; i=0
tab=$(printf '\t')
while IFS="$tab" read -r rt name cmd; do
    [ $((i % n)) = "$k" ] || { i=$((i + 1)); continue; }; i=$((i + 1))
    w=$(mktemp -d "$base/ltp-XXXXXX"); chmod 777 "$w"; o=$base/ltp-out.$$   # world-accessible like runltp's TMPDIR
    s=$(date +%s.%N)
    (cd "$w" && TMPDIR=$w exec timeout -s KILL "$OUTER" sh -c "$cmd") > "$o" 2>&1 < /dev/null; rc=$?
    e=$(date +%s.%N)
    echo "CT-RESULT ltp $rt:$name rc=$rc sec=$(echo "$s $e" | awk '{printf "%.2f", $2-$1}')"
    case $rc in 0|32) ;; *) tail -n 40 "$o" | sed "s/^/CT-OUT $rt:$name | /";; esac
    rm -rf "$w" "$o"
done < "$D/ltp-list.txt"
