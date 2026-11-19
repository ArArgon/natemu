#!/bin/sh
# cases.sh LIST PARTS: ltp-cases.txt from ltp-list.txt (RUNTEST<TAB>NAME<TAB>COMMAND, ltp-list.py's output and
# run-ltp.sh's input). One `PART TEST` line per entry: TEST = RUNTEST:NAME as run-ltp.sh's CT-RESULT line names it, and
# PART = K/PARTS, the part= word whose boot runs it. run-ltp.sh DIR K/N runs entries K, K+N, ... (counted from 0 over
# every line), so entry i (from 0) is in part (i mod PARTS). testkit/tests/test_campaign_ltp.py runs run-ltp.sh itself
# against this split.
awk -F'\t' -v n="$2" '{print (NR - 1) % n "/" n, $1 ":" $2}' "$1"
