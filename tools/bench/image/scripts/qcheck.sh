#!/bin/sh
# qcheck.sh CMD...: run CMD only when no natemu or QEMU process is running (another agent measures on this Mac).
if pgrep -f natemu-host >/dev/null || pgrep -fx '.*/natemu .*' >/dev/null || pgrep -f qemu-system >/dev/null; then
    echo "busy: an emulator is running; not started"; exit 75
fi
exec "$@"
