#!/bin/bash
# x86-campaign.sh OUT QUEUE: campaign.sh's loop (one vm.sh boot per QUEUE line, lines already reached skipped), but
# before each launch wait while the gather fix's full suite runs (its serial steps need the VM lock for seconds each and
# would wait behind a long boot: coordinator's scheduling request, November 6): sleep 30, at most 90 minutes.
here=$(dirname "$(realpath "$0")"); out=$1
while read -r name emu n suite part img; do
    case $name in ""|\#*) continue;; esac
    grep -q "\"name\": \"$name\", .*\"reached\": true" "$out/runs.jsonl" 2>/dev/null && continue
    for i in $(seq 1 180); do pgrep -f 'run_suite.py --fexc .*series66-gather' >/dev/null || break; sleep 30; done
    echo "$(date +%T) start $name" >> "$out/campaign.log"
    "$here/vm.sh" "$out" "$name" "$emu" "$n" "$suite" "$part" "$img" >> "$out/campaign.log" 2>&1
    echo "$(date +%T) done $name rc=$?" >> "$out/campaign.log"
done < "$2"
echo "$(date +%T) CAMPAIGN-END" >> "$out/campaign.log"
