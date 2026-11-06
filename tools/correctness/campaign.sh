#!/bin/bash
# campaign.sh OUT QUEUE: run each line of QUEUE (NAME EMU VCPUS SUITE PART INITRD) through vm.sh in turn, one boot at a
# time; a line already recorded as reached in OUT/runs.jsonl is skipped. Progress to OUT/campaign.log. Start it with
# nohup so it outlives the shell.
here=$(dirname "$(realpath "$0")"); out=$1
while read -r name emu n suite part img; do
    case $name in ""|\#*) continue;; esac
    grep -q "\"name\": \"$name\", .*\"reached\": true" "$out/runs.jsonl" 2>/dev/null && continue
    echo "$(date +%T) start $name" >> "$out/campaign.log"
    "$here/vm.sh" "$out" "$name" "$emu" "$n" "$suite" "$part" "$img" >> "$out/campaign.log" 2>&1
    echo "$(date +%T) done $name rc=$?" >> "$out/campaign.log"
done < "$2"
echo "$(date +%T) CAMPAIGN-END" >> "$out/campaign.log"
