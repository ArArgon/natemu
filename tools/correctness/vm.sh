#!/bin/bash
# vm.sh OUT NAME EMU VCPUS SUITE PART INITRD: one boot.py run under the shared VM lock, started only when no natemu or
# QEMU process is running (another agent measures on this Mac); waits and retries while busy (up to 2 hours).
# Defaults: the stub and runtime of the clean worktree natemu-raw/correct-us-20261105-01/wt, compiler series65-p2.
here=$(dirname "$(realpath "$0")"); RAW=${NATEMU_RAW_ROOT:-$here/../../../natemu-raw}; WT=${WT:-$RAW/correct-us-20261105-01/wt}
export STUB=${STUB:-$WT/target/release/natemu} RUNTIME=${RUNTIME:-$WT/target/aarch64-unknown-none/release/natemu-runtime}
export FEXC=${FEXC:-$RAW/fexc-native-20261026-01/series65-p2/obj/fexc}
"$here/yield.sh"
for try in $(seq 1 360); do
    rc=0; /usr/bin/lockf -k $RAW/.vm.lock "$here/idle-boot.sh" "$@" || rc=$?
    [ $rc = 75 ] || exit $rc; sleep 20
done
exit 75
