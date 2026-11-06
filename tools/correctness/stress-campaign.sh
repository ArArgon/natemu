#!/bin/bash
# stress-campaign.sh OUT QUEUE: x86-campaign.sh's loop (one vm.sh boot per QUEUE line, lines already reached skipped),
# waiting before each launch while any `run_suite.py` process runs (another agent's suite; sleep 30, at most 3 hours).
# A file named OUT/STOP ends the campaign before the next launch.
here=$(dirname "$(realpath "$0")"); out=$1
while read -r name emu n suite part img; do
    case $name in ""|\#*) continue;; esac
    [ -e "$out/STOP" ] && break
    grep -q "\"name\": \"$name\", .*\"reached\": true" "$out/runs.jsonl" 2>/dev/null && continue
    for i in $(seq 1 360); do pgrep -f 'run_suite.py' >/dev/null || break; sleep 30; done
    echo "$(date +%T) start $name" >> "$out/campaign.log"
    "$here/vm.sh" "$out" "$name" "$emu" "$n" "$suite" "$part" "$img" >> "$out/campaign.log" 2>&1
    echo "$(date +%T) done $name rc=$?" >> "$out/campaign.log"
done < "$2"
echo "$(date +%T) CAMPAIGN-END" >> "$out/campaign.log"
