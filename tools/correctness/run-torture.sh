#!/bin/sh
# run-torture.sh DIR [K/N]: run the gcc.c-torture/execute binaries of DIR/tests-*.tar.zst (chunks K, K+N, ...), each
# in an empty directory with a 120 s kill timeout. Prints one line per test:
#   CT-RESULT torture NAME.OPT rc=EXIT out=MD5-PREFIX-OF-STDOUT+STDERR
# Same script natively (DIR = the build's torture directory) and in the guest (DIR = /ct).
D=$1; k=${2%/*}; n=${2#*/}; [ -n "$2" ] || { k=0; n=1; }
T=${TMPDIR:-/tmp}/ct-torture; i=0
for c in "$D"/tests-*.tar.zst; do
    [ $((i % n)) = "$k" ] || { i=$((i + 1)); continue; }; i=$((i + 1))
    rm -rf "$T"; mkdir -p "$T/w"; zstd -q -dc --long=27 "$c" | tar -x -C "$T"
    for f in $(ls "$T" | sort); do
        [ -f "$T/$f" ] || continue
        (cd "$T/w" && exec timeout -s KILL 120 "$T/$f") > "$T/.out" 2>&1 < /dev/null; rc=$?
        echo "CT-RESULT torture $f rc=$rc out=$(md5sum < "$T/.out" | cut -c1-12)"
        rm -rf "$T/w" "$T/$f"; mkdir -p "$T/w"
    done
done
rm -rf "$T"
