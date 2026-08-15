#!/bin/bash
# run_flagtorture.sh -- run the same flag-exhaustive test vector generator under
# two independent x86-64 implementations available on this host and diff the
# per-group hashes:
#
#   A) Rosetta 2       (Apple's own x86-64 translator, uses hardware TSO + FlagM,
#                       i.e. the closest existing thing to what natemu will be)
#   B) QEMU 11 TCG     (full-system, the corpus baseline in doc 15)
#
# Neither is a specification, but they are independent implementations, and a
# disagreement between them is a bug in one of them.  When natemu exists it
# becomes side C and the same script covers it.
#
# Reuses probes/qemu_baseline/work/nb-x86_64/boot/vmlinuz-virt read-only and
# builds its own 2 MB initramfs under probes/oracle/work/.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK="$HERE/work"
QW="$HERE/../qemu_baseline/work"
CORPUS="${CORPUS:-64}"

mkdir -p "$WORK"
zig cc -target x86_64-macos      -O2         -o "$WORK/ft.macos-x64" "$HERE/flagtorture.c"
zig cc -target x86_64-linux-musl -O2 -static -o "$WORK/ft.linux-x64" "$HERE/flagtorture.c"

# --- side A: Rosetta 2 ------------------------------------------------------
echo "== A: Rosetta 2 (arch -x86_64), corpus=$CORPUS"
/usr/bin/time -p arch -x86_64 "$WORK/ft.macos-x64" --corpus "$CORPUS" \
    > "$WORK/ft.rosetta.txt" 2> "$WORK/ft.rosetta.time"
tail -2 "$WORK/ft.rosetta.time"

# --- side B: QEMU TCG full-system ------------------------------------------
RD="$WORK/rootfs"; rm -rf "$RD"; mkdir -p "$RD/proc" "$RD/sys" "$RD/dev"
cp "$WORK/ft.linux-x64" "$RD/ft"
cat > "$RD/init" <<EOF
#!/ft
EOF
# no shell in the image: run the binary directly as init, then power off
rm -f "$RD/init"
cat > "$WORK/initwrap.c" <<'EOF'
#include <unistd.h>
#include <sys/reboot.h>
int main(void){ char *a[]={"/ft","--corpus",CORPUS_STR,0};
    pid_t p=fork(); if(!p){execv("/ft",a);_exit(1);} int st; wait4(p,&st,0,0);
    sync(); reboot(RB_POWER_OFF); for(;;); }
EOF
zig cc -target x86_64-linux-musl -O2 -static -DCORPUS_STR="\"$CORPUS\"" \
       -o "$RD/init" "$WORK/initwrap.c" -Wno-implicit-function-declaration
( cd "$RD" && tar --format newc --uid 0 --gid 0 --uname root --gname root -cf "$WORK/ft.cpio" . )

echo "== B: QEMU 11 TCG full-system, corpus=$CORPUS"
/usr/bin/time -p qemu-system-x86_64 -accel tcg -machine q35 -cpu qemu64 -m 512 -smp 1 \
    -kernel "$QW/nb-x86_64/boot/vmlinuz-virt" -initrd "$WORK/ft.cpio" \
    -append "console=ttyS0 panic=-1 rdinit=/init loglevel=1" \
    -display none -nodefaults -monitor none -no-reboot -serial stdio \
    > "$WORK/ft.qemu.raw" 2> "$WORK/ft.qemu.time"
grep -E "^(FLAG|--|  )" "$WORK/ft.qemu.raw" | tr -d '\r' > "$WORK/ft.qemu.txt"
tail -2 "$WORK/ft.qemu.time"

# --- diff -------------------------------------------------------------------
echo "== diff"
if diff -u "$WORK/ft.rosetta.txt" "$WORK/ft.qemu.txt" > "$WORK/ft.diff"; then
    echo "IDENTICAL: Rosetta 2 and QEMU TCG agree on every group hash"
    grep FLAGTOTAL "$WORK/ft.rosetta.txt"
else
    echo "DIVERGENCE (this is the interesting case):"
    cat "$WORK/ft.diff"
fi
