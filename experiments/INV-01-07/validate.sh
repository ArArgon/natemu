#!/bin/sh
# Preflight for the code gate: builds the probe and exercises the checker
# against fixtures covering every integrity rule. Writes nothing outside a
# temp directory and produces no evidence.
set -eu
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
TMP_DIR=$(mktemp -d /tmp/inv-01-07-preflight.XXXXXX)
trap 'rm -rf "$TMP_DIR"' EXIT HUP INT TERM

sh -n "$SCRIPT_DIR/run.sh"
make -C "$SCRIPT_DIR" BUILD="$TMP_DIR/build"
"$TMP_DIR/build/timers" --control-selftest
python3 "$SCRIPT_DIR/src/check.py" "$SCRIPT_DIR/tests/valid.json"
python3 "$SCRIPT_DIR/tests/control-fixtures.py" \
    "$SCRIPT_DIR/src/check.py" "$SCRIPT_DIR/tests/valid.json"
# tests/negative.json is a recorded --negative run: it validates the rules the
# injected defect reaches. tests/negative-coverage.json validates the rest, so
# together they exercise every integrity rule (REV-045 required change 5; the
# per-rule split is tabulated in README.md).
for fixture in negative negative-coverage; do
    if python3 "$SCRIPT_DIR/src/check.py" "$SCRIPT_DIR/tests/$fixture.json"; then
        echo "$fixture checker fixture unexpectedly passed" >&2
        exit 1
    fi
done
echo "INV-01-07 preflight PASS"
