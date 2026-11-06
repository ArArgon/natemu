#!/bin/sh
# run-openssl.sh DIR [K/N]: OpenSSL's own test recipes (DIR/openssl-tests.txt) through its harness (test/run_tests.pl,
# the environment of `make test`), in the build tree OB (default /home/ubuntu/ct/ob, at the same path in the guest),
# once per capability mask of DIR/openssl-masks.txt (NAME OPENSSL_ia32cap, `-` = unset). Runs K, K+N, ... of the
# mask x recipe list.
#   CT-RESULT openssl MASK:RECIPE rc=EXIT ok=SUBTESTS-OK notok=SUBTESTS-NOT-OK
#   CT-OUT MASK:RECIPE | ...   the `not ok` lines (and the harness summary) of a recipe whose rc is not 0
D=$1; k=${2%/*}; n=${2#*/}; [ -n "$2" ] || { k=0; n=1; }
OB=${OB:-/home/ubuntu/ct/ob}; o=${TMPDIR:-/tmp}/ossl-out.$$; i=0
while read -r m cap; do
    while read -r t; do
        [ $((i % n)) = "$k" ] || { i=$((i + 1)); continue; }; i=$((i + 1))
        if [ "$cap" = - ]; then unset OPENSSL_ia32cap; else export OPENSSL_ia32cap=$cap; fi
        (cd "$OB" && SRCTOP=. BLDTOP=. PERL=perl FIPSKEY= EXE_EXT= HARNESS_JOBS=1 VERBOSE=1 \
            exec timeout -s KILL 3600 perl ./test/run_tests.pl "$t") > "$o" 2>&1 < /dev/null; rc=$?
        echo "CT-RESULT openssl $m:$t rc=$rc ok=$(grep -cE '^ *ok ' "$o") notok=$(grep -cE '^ *not ok ' "$o")"
        [ $rc = 0 ] || grep -E '^ *not ok |^Result|^Files=|Failed' "$o" | head -n 60 | sed "s|^|CT-OUT $m:$t \| |"
    done < "$D/openssl-tests.txt"
done < "$D/openssl-masks.txt"
unset OPENSSL_ia32cap; rm -f "$o"
