#!/bin/sh
set -eu
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
TMP_DIR=$(mktemp -d /tmp/inv-01-08-preflight.XXXXXX)
trap 'rm -rf "$TMP_DIR"' EXIT HUP INT TERM

sh -n "$SCRIPT_DIR/run.sh"
make -C "$SCRIPT_DIR" BUILD="$TMP_DIR/build"
make -C "$SCRIPT_DIR" BUILD="$TMP_DIR/build" disasm
for mnemonic in ldr str ldar stlr dmb ldaddal ldadd cas; do
    if ! grep -Eq "[[:space:]]$mnemonic[[:space:]]" "$TMP_DIR/build/litmus.disasm"; then
        echo "missing expected instruction in disassembly: $mnemonic" >&2
        exit 1
    fi
done
python3 "$SCRIPT_DIR/src/check.py" "$SCRIPT_DIR/tests/valid.json"
if python3 "$SCRIPT_DIR/src/check.py" "$SCRIPT_DIR/tests/negative.json"; then
    echo "negative checker fixture unexpectedly passed" >&2
    exit 1
fi
if python3 "$SCRIPT_DIR/src/check.py" "$SCRIPT_DIR/tests/forbidden.json"; then
    echo "forbidden-outcome checker fixture unexpectedly passed" >&2
    exit 1
fi
echo "INV-01-08 preflight PASS"
