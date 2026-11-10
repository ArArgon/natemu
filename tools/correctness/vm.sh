#!/bin/bash
# vm.sh OUT NAME EMU VCPUS SUITE PART INITRD: one boot.py run under the shared VM lock, started only when no natemu or
# QEMU process is running (another agent measures on this Mac); waits and retries while busy (up to 2 hours).
# Defaults: this checkout's release stub and runtime (WT overrides the tree), and the compiler pinned by the `F=` line of
# docs/restructure/boot-handoff.md (as tools/run_suite.py reads it).
here=$(dirname "$(realpath "$0")"); RAW=${NATEMU_RAW_ROOT:-$here/../../../natemu-raw}; WT=${WT:-$(realpath "$here/../..")}
export STUB=${STUB:-$WT/target/release/natemu} RUNTIME=${RUNTIME:-$WT/target/aarch64-unknown-none/release/natemu-runtime}
pinned=$(sed -n 's#^F=\.\./natemu-raw/\([^ ]*\).*#\1#p' "$here/../../docs/restructure/boot-handoff.md" | head -1)
export FEXC=${FEXC:-$RAW/$pinned}
"$here/yield.sh"
for try in $(seq 1 360); do
    rc=0; /usr/bin/lockf -k $RAW/.vm.lock "$here/idle-boot.sh" "$@" || rc=$?
    [ $rc = 75 ] || exit $rc; sleep 20
done
exit 75
