#!/bin/sh
# vms.sh: one line per running VM process (the natemu stub, the natemu-host test driver, qemu-system-x86_64), "PID PPID
# COMMAND"; nothing when none runs (exit 0 either way). The bracketed patterns keep this script's and pgrep's own command
# lines free of the names other agents' idle checks match.
for pid in $(pgrep -x 'natem[u]|natemu-hos[t]|qemu-system-x86_6[4]'); do
    ps -o pid=,ppid=,command= -p "$pid"
done
exit 0
