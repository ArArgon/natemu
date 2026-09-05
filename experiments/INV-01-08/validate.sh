#!/bin/sh
# Preflight for the code gate. Builds the probe, proves the litmus bodies emit
# the instructions their arms claim, and exercises the checker against fixtures
# that cover every integrity rule. Writes nothing outside a temp directory and
# produces no evidence.
set -eu
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
TMP_DIR=$(mktemp -d /tmp/inv-01-08-preflight.XXXXXX)
trap 'rm -rf "$TMP_DIR"' EXIT HUP INT TERM

sh -n "$SCRIPT_DIR/run.sh"
make -C "$SCRIPT_DIR" BUILD="$TMP_DIR/build"
make -C "$SCRIPT_DIR" BUILD="$TMP_DIR/build" disasm

# REV-045 required change 2: the instruction check must prove something about
# the litmus bodies specifically. `ldar`, `stlr`, `dmb`, and the LSE atomics
# also appear in the C11 sense-reversing barrier, so a whole-file grep - or
# even a grep of the _lit_thread range that lit_body inlines into - passes
# unchanged when every variant degenerates to plain LDR/STR. `make disasm`
# therefore also builds -DLITMUS_VERIFY, in which lit_body stays a separate
# symbol containing the bodies and nothing else.
awk '/^[0-9a-f]+ <_lit_body>:/{inrange=1;next} /^[0-9a-f]+ </{inrange=0} inrange' \
    "$TMP_DIR/build/litmus-verify.disasm" > "$TMP_DIR/lit_body.disasm"
if [ ! -s "$TMP_DIR/lit_body.disasm" ]; then
    echo "could not isolate the _lit_body symbol range in the verify build" >&2
    exit 1
fi
for mnemonic in ldr str ldar stlr dmb; do
    if ! grep -Eq "[[:space:]]$mnemonic[[:space:]]" "$TMP_DIR/lit_body.disasm"; then
        echo "litmus bodies do not emit $mnemonic; a variant has degenerated" >&2
        exit 1
    fi
done
# The atomics arms live in their own symbols.
for sym_mnemonic in '_lse_thread:ldadd' '_lse_thread:cas' '_probe_one:ldaddal'; do
    sym=${sym_mnemonic%%:*}
    mnemonic=${sym_mnemonic##*:}
    awk -v s="^[0-9a-f]+ <$sym>:" '$0 ~ s {inrange=1;next} /^[0-9a-f]+ </{inrange=0} inrange' \
        "$TMP_DIR/build/litmus.disasm" > "$TMP_DIR/sym.disasm"
    if ! grep -Eq "[[:space:]]$mnemonic[[:space:]]" "$TMP_DIR/sym.disasm"; then
        echo "$sym does not emit $mnemonic" >&2
        exit 1
    fi
done

python3 "$SCRIPT_DIR/src/check.py" "$SCRIPT_DIR/tests/valid.json"
python3 "$SCRIPT_DIR/tests/histogram-fixtures.py"
for fixture in negative forbidden negative-coverage; do
    if python3 "$SCRIPT_DIR/src/check.py" "$SCRIPT_DIR/tests/$fixture.json"; then
        echo "$fixture checker fixture unexpectedly passed" >&2
        exit 1
    fi
done
echo "INV-01-08 preflight PASS"
