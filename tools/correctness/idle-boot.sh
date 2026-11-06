#!/bin/sh
# idle-boot.sh ARGS...: exec boot.py ARGS only when no emulator process runs (exit 75 otherwise). A script file, so the
# patterns below never appear in a process's command line (they would match other agents' idle checks, and ours).
if pgrep -f natemu-host >/dev/null || pgrep -fx '.*/natemu .*' >/dev/null || pgrep -f qemu-system >/dev/null; then exit 75; fi
exec python3 "$(dirname "$0")/boot.py" "$@"
